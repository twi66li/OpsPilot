"""访客走原有登录与会话校验，独立示例只读；全程不调用模型。"""

import pytest

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models import ApprovalRequest, ChatSession, SupportCase
from app.services.visitor_service import VISITOR_ID, VISITOR_PASSWORD, VISITOR_TENANT, VISITOR_USERNAME, ensure_visitor
from tests.conftest import register


def visitor_login(client):
    response = client.post('/auth/login', json={'username': VISITOR_USERNAME, 'password': VISITOR_PASSWORD})
    assert response.status_code == 200, response.text
    auth = response.json()
    assert auth['is_guest'] and auth['role'] == 'viewer'
    return {'Authorization': 'Bearer ' + auth['access_token']}


def test_visitor_has_examples_and_seeding_is_idempotent(client):
    assert client.get('/auth/options').json()['visitor'] == {'username': VISITOR_USERNAME, 'password': VISITOR_PASSWORD}
    headers = visitor_login(client)
    assert client.get('/auth/me', headers=headers).json()['is_guest'] is True
    sites = client.get('/api/support/sites', headers=headers).json()
    assert len(sites) == 3
    primary = next(site for site in sites if site['id'] == 'visitor-example-site')
    assert '教学' in primary['name'] and len(primary.get('probe_history', [])) >= 10
    assert len(primary.get('doc_ids', [])) >= 2
    docs = client.get('/api/workspace/documents', headers=headers).json()['items']
    assert len(docs) == 3 and all(doc['active_version_id'] for doc in docs)
    timeline = client.get('/api/maintenance/sites/visitor-example-site/timeline', headers=headers).json()['events']
    assert sum(ev['kind'] == 'site_change' for ev in timeline) >= 3
    cases = client.get('/api/workspace/cases', headers=headers).json()['items']
    assert len(cases) >= 14 and all('示例' in case['title'] for case in cases)
    assert {case['created_at'][5:7] for case in cases} == {'08', '09', '10', '11'}
    detail = client.get('/api/support/cases/visitor-example-502', headers=headers).json()
    assert detail['evidence'][0]['summary']['http_statuses'][0]['status'] == 502
    assert detail['report']['observations'][0]['evidence_id'] == 'E1'
    assert client.get('/api/support/library', headers=headers).json()
    with SessionLocal() as db:
        session_count = db.query(ChatSession).filter_by(user_id=VISITOR_ID).count()
        approval_count = db.query(ApprovalRequest).filter_by(tenant_id=VISITOR_TENANT).count()
        ensure_visitor(db)
        assert db.query(ChatSession).filter_by(user_id=VISITOR_ID).count() == session_count
        assert db.query(SupportCase).filter_by(user_id=VISITOR_ID).count() == len(cases)
        assert db.query(ApprovalRequest).filter_by(tenant_id=VISITOR_TENANT).count() == approval_count


@pytest.mark.parametrize('method,path,body', [
    ('POST', '/api/support/sites', {'name': '不能新增'}),
    ('POST', '/api/support/cases', {'title': '不能新增'}),
    ('DELETE', '/api/support/cases/visitor-example-502', None),
    ('POST', '/api/agent/chat', {'session_id': 'visitor-example-502', 'message': '不能调用模型'}),
    ('POST', '/auth/password', {'current_password': VISITOR_PASSWORD, 'new_password': 'changed-pass-123'}),
    ('POST', '/api/maintenance/retrieval-test', {'query': '不能消耗模型额度', 'doc_ids': [], 'include_official': True}),
    ('POST', '/api/sessions/delete', {'ids': ['visitor-example-502']}),
    ('PUT', '/api/maintenance/cases/visitor-example-502/metadata', {'severity': 'P0', 'labels': []}),
    ('POST', '/api/agent/approvals/visitor-demo-export-approval/decide', {'approved': True}),
])
def test_visitor_cannot_write_or_start_analysis(client, method, path, body):
    response = client.request(method, path, headers=visitor_login(client), json=body)
    assert response.status_code == 403
    assert '只读体验' in response.json()['detail']


def test_visitor_cannot_read_personal_data_or_upload(client):
    personal = register(client, 'visitor_isolation_owner', 'pass123456')
    own_site = client.post('/api/support/sites', headers=personal, json={'name': '私人网站'}).json()
    visitor = visitor_login(client)
    assert client.get('/api/support/sites/' + own_site['id'], headers=visitor).status_code == 404
    assert client.get('/api/support/sites/visitor-example-site', headers=personal).status_code == 404
    upload = client.post('/api/support/documents/upload', headers=visitor,
                         files={'file': ('manual.txt', b'cannot upload', 'text/plain')})
    assert upload.status_code == 403


def test_visitor_reads_only_published_demo_approvals_and_evaluations(client):
    headers = visitor_login(client)
    with SessionLocal() as db:
        db.add(ApprovalRequest(id="visitor-test-unpublished", tenant_id=VISITOR_TENANT,
                               session_id="private-sample", ticket_draft={"title": "内部讨论"}))
        db.commit()
    approvals = client.get('/api/workspace/approvals', headers=headers).json()['items']
    assert len(approvals) == 12
    assert {row['status'] for row in approvals} == {'approved', 'pending', 'rejected'}
    assert {row['created_at'][5:7] for row in approvals} == {'08', '09', '10', '11'}
    tickets = client.get('/api/workspace/tickets', headers=headers).json()['items']
    assert {row['approval_id'] for row in tickets} == {row['id'] for row in approvals if row['status'] == 'approved'}
    detail = client.get('/api/maintenance/operations/' + approvals[0]['id'], headers=headers).json()
    assert detail['record']['session_id'] and detail['run'] is None
    assert client.get('/api/maintenance/operations/visitor-test-unpublished', headers=headers).status_code == 404
    runs = client.get('/api/eval/runs?include_development=true', headers=headers).json()['runs']
    assert {row['id'][-2:] for row in runs} == {'08', '09', '10', '11'}
    assert [row['question_count'] for row in runs] == [12, 9, 6, 3]
    report = client.get('/api/eval/results', params={'run': runs[0]['id']}, headers=headers).json()
    assert report['summary']['口径'] == '合成演示样本' and report['questions']
    assert client.get('/api/eval/results?run=night20261031/real_run', headers=headers).status_code == 404
    compare = client.get('/api/eval/compare', params={'baseline': runs[-1]['id'], 'candidate': runs[0]['id']}, headers=headers)
    assert compare.json()['common_questions'] == 3


def test_visitor_demo_conversations_are_read_only_and_outside_chat_quota(client):
    headers = visitor_login(client)
    threads = client.get('/api/assistant/conversations', headers=headers).json()
    demo = [thread for thread in threads if thread.get('read_only')]
    assert len(demo) == 12 and {thread['created_at'][5:7] for thread in demo} == {'08', '09', '10', '11'}
    url = '/api/assistant/conversations/' + demo[-1]['id']
    detail = client.get(url, headers=headers).json()
    assert detail['read_only'] and len(detail['messages']) == 6
    times = [item['created_at'] for item in detail['messages']]
    assert times == sorted(set(times))
    assert len({thread['created_at'][11:] for thread in demo}) == len(demo)
    assert client.patch(url, json={'title': '不可修改'}, headers=headers).status_code == 404
    assert client.delete(url, headers=headers).status_code == 404
    assert client.get('/api/assistant/usage', headers=headers).json()['chats']['used'] == 0


def test_visitor_drill_replay_links_run_and_approved_samples(client, admin_headers, enable_drill, monkeypatch):
    with SessionLocal() as db:
        ensure_visitor(db)
        ensure_visitor(db)
    headers = visitor_login(client)
    drills = client.get('/api/workspace/sessions', headers=headers).json()['items']
    assert len(drills) == 4 and {row['created_at'][5:7] for row in drills} == {'08', '09', '10', '11'}
    latest = drills[0]['id']
    history = client.get('/api/sessions/' + latest, headers=headers).json()
    assert len(history['messages']) == 6
    run_url = '/api/agent/runs/' + history['messages'][-1]['meta']['run_id']
    run = client.get(run_url, headers=headers).json()
    assert run['data']['demo'] and run['status'] == 'completed'
    events = run['data']['events']
    assert len(events) == 6 and events[3]['detail']['result']['decision'] == 'approved'
    operation = run['data']['demo_operation_id']
    detail = client.get('/api/maintenance/operations/' + operation, headers=headers).json()
    assert detail['record']['session_id'] == latest and detail['kind'] == 'remediation'
    assert [probe['status'] for probe in detail['record']['result']['after']] == [200, 200, 200]
    assert client.post(run_url + '/cancel', headers=headers).status_code == 404
    assert client.get(run_url, headers=admin_headers).status_code == 404
    monkeypatch.setattr(get_settings(), 'drill_enabled', False)
    assert client.get('/api/workspace/sessions', headers=headers).json()['items'] == []
    assert client.get(run_url, headers=headers).status_code == 404


def test_visitor_logout_only_revokes_current_session(client):
    first, second = visitor_login(client), visitor_login(client)
    assert client.post('/auth/logout', headers=first).status_code == 200
    assert client.get('/auth/me', headers=first).status_code == 401
    assert client.get('/auth/me', headers=second).status_code == 200


def test_disabled_visitor_hides_entry_and_rejects_existing_token(client, monkeypatch):
    visitor = visitor_login(client)
    monkeypatch.setattr(get_settings(), 'visitor_enabled', False)
    assert client.get('/auth/options').json()['visitor'] is None
    assert client.post('/auth/login', json={'username': VISITOR_USERNAME, 'password': VISITOR_PASSWORD}).status_code == 403
    assert client.get('/auth/me', headers=visitor).status_code == 403
    assert client.post('/auth/logout', headers=visitor).status_code == 200
