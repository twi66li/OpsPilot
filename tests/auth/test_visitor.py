"""访客走原有登录与会话校验，独立示例只读；全程不调用模型。"""

import pytest

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models import ChatSession, SupportCase
from app.services.visitor_service import VISITOR_ID, VISITOR_PASSWORD, VISITOR_USERNAME, ensure_visitor
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
    assert len(sites) == 1 and '教学' in sites[0]['name']
    cases = client.get('/api/workspace/cases', headers=headers).json()['items']
    assert len(cases) == 2 and all('示例' in case['title'] for case in cases)
    detail = client.get('/api/support/cases/visitor-example-502', headers=headers).json()
    assert detail['evidence'][0]['summary']['http_statuses'][0]['status'] == 502
    assert detail['report']['observations'][0]['evidence_id'] == 'E1'
    assert client.get('/api/support/library', headers=headers).json()
    with SessionLocal() as db:
        ensure_visitor(db)
        assert db.query(ChatSession).filter_by(user_id=VISITOR_ID).count() == 2
        assert db.query(SupportCase).filter_by(user_id=VISITOR_ID).count() == 2


@pytest.mark.parametrize('method,path,body', [
    ('POST', '/api/support/sites', {'name': '不能新增'}),
    ('POST', '/api/support/cases', {'title': '不能新增'}),
    ('DELETE', '/api/support/cases/visitor-example-502', None),
    ('POST', '/api/agent/chat', {'session_id': 'visitor-example-502', 'message': '不能调用模型'}),
    ('POST', '/auth/password', {'current_password': VISITOR_PASSWORD, 'new_password': 'changed-pass-123'}),
    ('POST', '/api/maintenance/retrieval-test', {'query': '不能消耗模型额度', 'doc_ids': [], 'include_official': True}),
    ('POST', '/api/sessions/delete', {'ids': ['visitor-example-502']}),
    ('PUT', '/api/maintenance/cases/visitor-example-502/metadata', {'severity': 'P0', 'labels': []}),
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
