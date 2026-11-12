"""网站业务、任务分流与程序核对的最小验收。"""
import json

from app.container import get_container
from app.services.evidence_checks import analyze_samples, measure
from app.services.support_service import choose_workflow
from tests.conftest import register


def samples(path="/orders", after_path=None):
    return {"plan": {"revision": 1, "evidence_ids": ["before"]}, "evidence": [
        {"id": "before", "kind": "initial", "text": f"2026-11-12T10:00:00+08:00 POST {path} HTTP 502"},
        {"id": "after", "kind": "check_result", "plan_revision": 1,
         "text": "\n".join(f'2026-11-12T10:10:0{i}+08:00 POST {after_path or path} HTTP/1.1" 200 elapsed=0.12s' for i in range(3))},
    ]}


def test_sample_comparison_does_not_confuse_endpoints_times_or_container_running():
    context = samples()
    result = analyze_samples(context)
    assert result["verification"]["verdict"] == "sample_recovered"
    assert result["comparison"]["after_requests"] == 3 and result["comparison"]["after_failures"] == 0
    assert result["measurements"][1]["requests"][0]["elapsed_ms"] == 120
    assert analyze_samples(samples(after_path="/other"))["verification"]["verdict"] == "inconclusive"
    context["evidence"][1]["text"] = '{"Status":"running","OOMKilled":false}'
    assert analyze_samples(context)["verification"]["verdict"] == "inconclusive"
    context["evidence"][1]["text"] = '{"Status":"exited","OOMKilled":true}'
    assert analyze_samples(context)["verification"]["verdict"] == "still_failing"
    context = samples()
    context["evidence"][1]["text"] = context["evidence"][1]["text"].replace("10:10", "09:10")
    assert analyze_samples(context)["verification"]["verdict"] == "inconclusive"
    context["evidence"][1]["text"] = "2026-99-99T10:00:00+08:00 POST /orders HTTP 200"
    assert analyze_samples(context)["verification"]["verdict"] == "inconclusive"
    context = samples()
    context["evidence"][1]["text"] += "\nsqlalchemy.exc.TimeoutError: QueuePool connection timed out"
    assert analyze_samples(context)["verification"]["verdict"] == "still_failing"
    assert measure({"id":"note", "kind":"initial", "text":"本次日志未再出现QueuePool timeout"})["pool_error_lines"] == []
    nginx = measure({"id":"nginx", "kind":"initial", "text":'127.0.0.1 - - [12/Nov/2026:10:00:00 +0800] "GET /checkout HTTP/1.1" 502 100'})
    assert nginx["requests"][0]["at"] == "2026-11-12T10:00:00+08:00"
    redirect = samples()
    redirect["evidence"][1]["text"] = redirect["evidence"][1]["text"].replace('200 elapsed','302 elapsed')
    assert analyze_samples(redirect)["verification"]["verdict"] == "inconclusive"


def test_support_workflows_have_separate_tasks():
    assert choose_workflow("Docker怎么查看日志？", {}) == "consult"
    assert choose_workflow("网站返回502，这是错误日志", {}) == "diagnose"
    assert choose_workflow("上次这个问题如何处理？", {}) == "history"
    assert choose_workflow("请核对新结果", {"plan":{"revision":1}}) == "review"
    assert choose_workflow("你好", {}) == "welcome"
    assert choose_workflow("核对结果", {}, "review") == "diagnose"


def test_site_issues_lifecycle_and_step_checks_are_independent(client, admin_headers):
    outsider = register(client, "workspace-outsider", "user123456")
    site = client.post('/api/support/sites',headers=admin_headers,json={"name":"维护平台验收网站","deployment":"Docker"}).json()
    conversation = client.post('/api/support/cases',headers=admin_headers,json={"title":"学习Docker日志","kind":"consult","site_id":site["id"]}).json()
    assert conversation['id'] not in {c['id'] for c in client.get('/api/support/cases?kind=problem',headers=admin_headers).json()}
    case = client.post('/api/support/cases',headers=admin_headers,json={"title":"网站502","site_id":site["id"],"symptom":"访问结算页失败","endpoint":"/checkout"}).json()
    root='/api/support/cases/'+case['id']
    detail=client.get('/api/support/sites/'+site['id'],headers=admin_headers).json()
    assert [c['id'] for c in detail['cases']] == [case['id']]
    assert client.get('/api/support/sites/'+site['id'],headers=outsider).status_code == 404
    assert client.put(root+'/problem',headers=outsider,json={"impact":"修改他人问题"}).status_code == 404
    assert client.post(root+'/status',headers=admin_headers,json={"status":"user_resolved"}).status_code == 400
    assert client.post(root+'/status',headers=admin_headers,json={"status":"sample_recovered"}).status_code == 400
    # 归档一个未解决的问题不推断已解决；重新打开保留证据和网站关联。
    from app.db.base import SessionLocal
    from app.db.models import SupportCase
    with SessionLocal() as db:
        row=db.get(SupportCase,case['id'])
        row.data={**row.data,"report":{"summary":"需要补充日志","steps":[{"location":"网关","action":"读取error日志","normal":"保留请求时间","abnormal":"补充上游异常","risk":"只读","rollback":""}]},"revision":1}
        db.commit()
    assert client.post(root+'/confirm-plan',headers=admin_headers).status_code == 200
    checked=client.post(root+'/plan/steps/0',headers=admin_headers,json={"revision":1,"status":"done","note":"已收集日志"}).json()
    assert checked['plan']['checks'][0]['status']=='done' and checked['sample_checks']['verification']['verdict']=='inconclusive'
    archived=client.post(root+'/save',headers=admin_headers,json={"solution":"缺少上游日志，暂时归档","user_confirmed":True}).json()
    assert archived['archived'] and archived['status'] != 'user_resolved'
    assert client.post(root+'/evidence',headers=admin_headers,json={"text":"new"}).status_code == 409
    reopened=client.post(root+'/status',headers=admin_headers,json={"status":"reopen"}).json()
    assert not reopened['archived'] and reopened['site_id']==site['id']
    resolved=client.post(root+'/status',headers=admin_headers,json={"status":"user_resolved","note":"用户已恢复原代理配置并验证","user_confirmed":True}).json()
    assert resolved['status']=='user_resolved' and not resolved['archived']
    assert client.post('/api/agent/chat',headers=admin_headers,json={"session_id":case['id'],"message":"继续排查"}).status_code == 409


def test_knowledge_consultation_does_not_generate_recovery_or_an_issue(client, admin_headers, monkeypatch):
    case=client.post('/api/support/cases',headers=admin_headers,json={"title":"如何看日志","kind":"consult"}).json()
    class Model:
        def tool_turn(self,messages,tools,**kwargs):
            assert {t['function']['name'] for t in tools} == {'search_runbook','get_site_overview'}
            return {'role':'assistant','content':json.dumps({'summary':'在容器主机使用docker logs查看标准输出日志。','references':[]})}
    monkeypatch.setattr(get_container().llm,'_api',Model(),raising=False)
    response=client.post('/api/agent/chat',headers=admin_headers,json={"session_id":case['id'],"message":"Docker怎么查看日志","workflow":"consult"})
    assert response.status_code==200,response.text
    report=response.json()['support_report']
    assert report['workflow']=='consult' and not report['steps']
    assert '结果核对' not in response.json()['answer']
    records=client.get('/api/support/cases?kind=problem',headers=admin_headers).json()
    assert case['id'] not in {r['id'] for r in records}
