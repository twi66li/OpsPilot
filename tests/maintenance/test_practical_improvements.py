import json

from app.db.base import SessionLocal
from app.db.models import AgentRun, SupportCase, WebsiteProfile
from app.services.support_service import context_for_session
from tests.conftest import register


def make_site(client, headers, **values):
    response = client.post('/api/support/sites', headers=headers, json={'name': '业务网站', **values})
    assert response.status_code == 200, response.text
    return response.json()


def test_site_metadata_filtering_dependencies_and_old_update_preserve_snapshot(client):
    headers = register(client, 'practical_sites', 'pass123456')
    site = make_site(client, headers, environment='production', group='订单组', owner='小王',
                     dependencies=[{'source': 'Nginx', 'target': 'app', 'note': '反向代理'}, {'source': 'app', 'target': 'PostgreSQL'}])
    other = make_site(client, headers, environment='test', group='会员组')
    query = client.get('/api/workspace/sites?environment=production&group=订单&owner=小王', headers=headers).json()
    assert [row['id'] for row in query['items']] == [site['id']]
    assert client.get('/api/workspace/sites?q=会员组', headers=headers).json()['items'][0]['id'] == other['id']
    case = client.post('/api/support/cases', headers=headers, json={'title': '原环境快照', 'site_id': site['id']}).json()
    with SessionLocal() as db:
        row = db.get(WebsiteProfile, site['id'])
        row.data = {**row.data, 'health_check': {'state': 'unknown'}}
        db.commit()
    updated = client.put('/api/support/sites/'+site['id'], headers=headers, json={'name': '新名称'}).json()
    assert updated['environment'] == 'production' and len(updated['dependencies']) == 2
    assert updated['health_check'] == {'state': 'unknown'}
    assert client.get('/api/support/cases/'+case['id'], headers=headers).json()['profile']['name'] == '业务网站'
    with SessionLocal() as db:
        row = db.get(WebsiteProfile, other['id'])
        row.data = {'name': '旧版网站', 'doc_ids': []}
        db.commit()
    assert client.get('/api/workspace/sites?environment=unknown', headers=headers).json()['items'][0]['id'] == other['id']


def test_dependency_input_is_validated_and_redacted(client, admin_headers):
    assert client.post('/api/support/sites', headers=admin_headers, json={'dependencies': [{'source': 'app', 'target': 'app'}]}).status_code == 422
    assert client.post('/api/support/sites', headers=admin_headers, json={'dependencies': [{'source': ' ', 'target': 'db'}]}).status_code == 422
    site = make_site(client, admin_headers, dependencies=[{'source': 'app', 'target': 'db', 'note': 'password=private-secret'}])
    assert 'private-secret' not in json.dumps(site)


def test_changes_compare_occurrence_time_with_saved_logs_and_reach_model_context(client):
    headers = register(client, 'practical_changes', 'pass123456')
    site = make_site(client, headers)
    case = client.post('/api/support/cases', headers=headers, json={'site_id': site['id'], 'title': '变更后报错'}).json()
    sid = case['id']
    client.post(f'/api/support/cases/{sid}/evidence', headers=headers,
                json={'text': '2026-11-01T10:00:00+08:00 GET /orders HTTP 502', 'label': '现场日志'})
    path = f'/api/maintenance/sites/{site["id"]}/changes'
    response = client.post(path, headers=headers, json={'change_kind': 'release', 'title': '发布新版本',
                           'version': 'v2', 'occurred_at': '2026-11-01T09:55:00+08:00', 'note': 'password=hidden-value'})
    assert response.status_code == 201
    assert client.post(path, headers=headers, json={'change_kind': 'config', 'title': '缺少时区', 'occurred_at': '2026-11-01T09:00:00'}).status_code == 422
    for route in [f'/api/maintenance/sites/{site["id"]}/timeline', f'/api/maintenance/cases/{sid}/timeline']:
        timeline = client.get(route, headers=headers).json()
        related = [item for item in timeline['events'] if item['kind'] in ['site_change', 'evidence_observed']]
        assert [item['kind'] for item in related] == ['site_change', 'evidence_observed']
        assert related[0]['at'] == '2026-11-01T01:55:00+00:00'
        assert 'hidden-value' not in json.dumps(timeline)
        assert '因果' in related[0]['detail']['source']
    with SessionLocal() as db:
        row = db.get(SupportCase, sid)
        context = context_for_session(db, sid, row.tenant_id, row.user_id)
        assert context['recent_changes'][0]['detail']['title'] == '发布新版本'
    exported = client.get(f'/api/maintenance/cases/{sid}/report', headers=headers).text
    assert '发布新版本' in exported and '网站变更（手工记录）' in exported


def test_new_site_functions_keep_user_isolation_and_visitor_read_only(client, admin_headers):
    headers = register(client, 'practical_other', 'pass123456')
    site = make_site(client, headers, group='私人业务组')
    path = f'/api/maintenance/sites/{site["id"]}/changes'
    assert client.post(path, headers=admin_headers, json={'change_kind': 'config', 'title': '越权', 'occurred_at': '2026-11-01T00:00:00Z'}).status_code == 404
    assert client.get(f'/api/maintenance/sites/{site["id"]}/timeline', headers=admin_headers).status_code == 404
    assert not client.get('/api/workspace/sites?group=私人业务组', headers=admin_headers).json()['items']
    visitor = client.post('/auth/login', json=client.get('/auth/options').json()['visitor']).json()
    guest = {'Authorization': 'Bearer '+visitor['access_token']}
    assert client.post('/api/maintenance/sites/visitor-example-site/changes', headers=guest,
                       json={'change_kind': 'config', 'title': '禁止修改', 'occurred_at': '2026-11-01T00:00:00Z'}).status_code == 403


def test_capabilities_are_truthful_private_and_do_not_echo_configuration(client, monkeypatch):
    from app.config import get_settings

    headers = register(client, 'practical_capabilities', 'pass123456')
    record = client.post('/api/support/cases', headers=headers, json={'title': '本人的失败记录'}).json()
    with SessionLocal() as db:
        row = db.get(SupportCase, record['id'])
        db.add(AgentRun(id='practical-run', tenant_id=row.tenant_id, session_id=row.session_id,
                        mode='rule', status='failed', data={'question': '检查请求', 'result': {'end_reason': 'model_error', 'message': 'private-error-secret'}}))
        db.commit()
    result = client.get('/api/maintenance/capabilities', headers=headers).json()
    assert result['items'][0]['state'] == 'offline'
    assert result['items'][2]['state'] == 'manual'
    assert result['recent'][0]['reason'] == '模型请求未完成'
    assert 'private-error-secret' not in json.dumps(result)
    other = register(client, 'practical_capabilities_other', 'pass123456')
    assert not client.get('/api/maintenance/capabilities', headers=other).json()['recent']
    monkeypatch.setattr(get_settings(), 'llm_provider', 'openai')
    monkeypatch.setattr(get_settings(), 'openai_api_key', 'private-model-key')
    configured = client.get('/api/maintenance/capabilities', headers=headers).json()
    assert configured['items'][0]['state'] == 'configured'
    assert '未发起模型请求' in configured['items'][0]['description']
    assert 'private-model-key' not in json.dumps(configured)
