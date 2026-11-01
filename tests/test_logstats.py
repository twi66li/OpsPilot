"""确定性日志统计（P1-ARAG-20261102）离线验收：全流程无 Docker、无真实 API。

覆盖任务书最小验收：
1. 已知数量、不同级别、完整行与残行重复的小样本：程序统计与手算期望一致，残行不覆盖完整原文；
2. 缺少目标行的样本：不产出全范围精确计数（missing 行段显式报告）；
3. "出现最多"等无法确定性解析的口径：列入 unsupported，不猜口径；
4. 真实编译图（DiagState）到 final 保留新增统计/证据字段；
5. 完整图离线链路（MockLLM + 内存向量库）：统计工具事件与 range_stats 进入最终响应。
"""
import json

from app.agent.llm import diagnosis_system_prompt
from app.rag.logstats import compute_range_stats, parse_conditions
from app.rag.evidence import _resolve_target_lines

# ---- 样本 A：已知计数、混合级别、残行与完整行重复 ----
DOC_TITLE = "BGL 测试样本 L100-L110"
DOC_LINES = "\n".join([
    "L100: 1117838677 2005.06.03 N1 2005-06-03-15.44.37.999605 N1 RAS KERNEL INFO instruction cache parity error corrected",
    "L101: 1117838678 2005.06.03 N1 2005-06-03-15.44.38.154501 N1 RAS KERNEL INFO instruction cache parity error corrected",
    "L102: 1117838678 2005.06.03 N1 2005-06-03-15.44.38.304938 N1 RAS KERNEL INFO instruction cache parity error corrected",
    "L103: 1117838678 2005.06.03 N2 2005-06-03-15.44.38.448810 N2 RAS APP FATAL data TLB error interrupt",
    "L104: 1117838679 2005.06.03 N2 2005-06-03-15.44.39.045415 N2 RAS KERNEL FATAL instruction TLB error interrupt",
    "L105: 1117838679 2005.06.03 N1 2005-06-03-15.44.39.111111 N1 RAS KERNEL INFO instruction cache parity error corrected",
    "L106: 1117838680 2005.06.03 N2 2005-06-03-15.44.39.222222 N2 RAS KERNEL FATAL data TLB error interrupt",
    "L107: 1117838680 2005.06.03 N2 2005-06-03-15.44.39.333333 N2 RAS APP FATAL data TLB error interrupt",
    "L108: 1117838681 2005.06.03 N1 2005-06-03-15.44.39.444444 N1 RAS KERNEL INFO instruction cache parity error corrected",
    "L109: 1117838681 2005.06.03 N3 2005-06-03-15.44.39.555555 N3 RAS KERNEL FATAL data TLB error interrupt",
    "L110: 1117838682 2005.06.03 N1 2005-06-03-15.44.39.666666 N1 RAS KERNEL INFO instruction cache parity error corrected",
])
DOC = [(DOC_TITLE, DOC_LINES + "\n")]
# 手算期望：总行 11；parity 子串 6（L100,101,102,105,108,110）；data TLB 子串 4；
# 级别 FATAL 5（L103,104,106,107,109）；INFO 6；时间范围 L100~L110。


def _hits_with_truncated_tail() -> list[dict]:
    """命中块：末尾带 L105 的截断残行（无换行结尾），模拟旧分块边界。"""
    chunk1 = "\n".join(DOC_LINES.splitlines()[:5]) + "\n" + "L105: 1117838679 2005.06.03 N1 2005-06-03-15.44.39.1"
    chunk2 = "rrected\n" + "\n".join(DOC_LINES.splitlines()[6:]) + "\n"
    return [
        {"doc_title": DOC_TITLE, "chunk_id": "t::0", "score": 0.9, "text": chunk1},
        {"doc_title": DOC_TITLE, "chunk_id": "t::1", "score": 0.8, "text": chunk2},
    ]


Q_COUNT = "L100-L110 中 data TLB error interrupt 消息共多少条？"
Q_PARITY = "L100-L110 中 instruction cache parity error corrected 消息出现多少次？"
Q_LEVEL = "L100-L110 中 FATAL 级别的日志有多少条？"
Q_MODE = "L100-L110 中出现最多的是哪条消息？"
Q_TIME = "L100-L110 中第一条与最后一条消息的精确时间戳分别是什么？"
Q_MISSING = "L200-L210 中 data TLB error interrupt 消息共多少条？"
DOC_MISSING = [("BGL 测试样本 L200-L205", "\n".join(
    f"L{n}: 1117838700 2005.06.03 N1 2005-06-03-16.00.00.00000{n} N1 RAS KERNEL FATAL data TLB error interrupt"
    for n in range(200, 206)) + "\n")]


def test_sample_a_counts_match_hand_computed():
    stats = compute_range_stats(Q_COUNT, _hits_with_truncated_tail(), docs=DOC)
    assert stats is not None
    assert stats["total_lines"] == 11
    sub = next(c for c in stats["conditions"] if c["kind"] == "substring" and "data TLB" in c["value"])
    assert sub["count"] == 4 and sub["line_numbers"] == [103, 106, 107, 109]
    assert stats["incomplete_excluded"] == []  # 文档为权威源，残行被完整原文覆盖


def test_sample_a_level_and_time():
    stats = compute_range_stats(Q_LEVEL, [], docs=DOC)
    lvl = next(c for c in stats["conditions"] if c["kind"] == "level" and c["value"] == "FATAL")
    assert lvl["count"] == 5 and lvl["line_numbers"] == [103, 104, 106, 107, 109]
    # 未在问题中提及的级别不得凭空出现在统计条件里（保守解析）
    assert all(c["value"] != "INFO" for c in stats["conditions"] if c["kind"] == "level")
    stats2 = compute_range_stats("L100-L110 中 INFO 级别的日志有多少条？", [], docs=DOC)
    info = next(c for c in stats2["conditions"] if c["kind"] == "level" and c["value"] == "INFO")
    assert info["count"] == 6
    tr = stats["time_range"]
    assert tr["first"] == "2005-06-03-15.44.37.999605" and tr["first_line"] == 100
    assert tr["last"] == "2005-06-03-15.44.39.666666" and tr["last_line"] == 110


def test_truncated_line_does_not_override_complete_and_is_excluded():
    # 无文档回退时：L105 只有块尾残行 → 计入 incomplete_excluded，且不进入任何计数
    stats = compute_range_stats(Q_PARITY, _hits_with_truncated_tail(), docs=[])
    assert stats["incomplete_excluded"] == [105]
    assert stats["total_lines"] == 10
    sub = next(c for c in stats["conditions"] if "parity" in c["value"])
    assert sub["count"] == 5 and 105 not in sub["line_numbers"]
    # 残行内容不得压过完整原文：解析结果中的 L105 来自文档时为完整行
    found, incomplete = _resolve_target_lines([105], _hits_with_truncated_tail(), DOC)
    assert incomplete == [] and "instruction cache parity error corrected" in found[105][1]


def test_sample_b_missing_targets_reported_not_counted_as_full():
    stats = compute_range_stats(Q_MISSING, [], docs=DOC_MISSING)
    assert stats["ranges"][0]["missing"] == list(range(206, 211))
    assert stats["missing_lines"] == list(range(206, 211))
    # 只统计已解析行，不得产出全范围（11 行）的精确总数
    assert stats["total_lines"] == 6
    sub = next(c for c in stats["conditions"] if "data TLB" in c["value"])
    assert sub["count"] == 6 and sub["line_numbers"] == list(range(200, 206))


def test_mode_condition_is_unsupported_not_guessed():
    conditions, unsupported, wants = parse_conditions(Q_MODE)
    assert wants["mode"] and unsupported
    stats = compute_range_stats(Q_MODE, [], docs=DOC)
    assert stats["unsupported"]
    assert all(c["kind"] != "mode" for c in stats["conditions"])


def test_no_count_object_parses_to_unsupported():
    stats = compute_range_stats("L100-L110 的日志都说了什么？", [], docs=DOC)
    # 无计数诉求时不得凭空造计数条件；没有无法解析的计数诉求也不报 unsupported
    assert stats["conditions"] == [] or all(c["kind"] != "count" for c in stats["conditions"])


def test_real_compiled_graph_retains_stats_fields_to_final():
    """实际 DiagState 编译图到 final：新增统计/覆盖字段不被丢弃（终审 P1-CITATIONS 同类缺陷防护）。"""
    from langgraph.graph import END, START, StateGraph

    from app.agent.state import DiagState

    def node(state):
        return {"range_stats": {"total_lines": 11}, "range_coverage": {"missing": [1]}, "answer": "x"}

    g = StateGraph(DiagState)
    g.add_node("n", node)
    g.add_edge(START, "n")
    g.add_edge("n", END)
    final = g.compile().invoke({})
    assert final["range_stats"] == {"total_lines": 11}
    assert final["range_coverage"] == {"missing": [1]}


def test_system_prompt_separates_stats_from_root_cause():
    prompt = diagnosis_system_prompt()
    assert "已知事实、可能解释与仍需取得的证据" in prompt
    assert "统计正确不等于根因已证实" in prompt
    assert "不得对缺失范围给出精确总数" in prompt
    assert "请其澄清口径" in prompt


def test_stats_flow_through_full_offline_graph(client, admin_headers):
    """完整编译图离线链路（MockLLM + 内存向量库）：统计工具事件与 range_stats 进入 final。"""
    doc = {"title": DOC_TITLE, "content": DOC_LINES, "source": "test"}
    resp = client.post("/api/kb/documents", json=doc, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    sid = client.post("/api/sessions", json={"title": "stats-e2e"}, headers=admin_headers).json()["id"]
    r = client.post("/api/agent/chat", json={"session_id": sid, "message": Q_COUNT}, headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    stats = body.get("range_stats")
    assert stats and stats["total_lines"] == 11
    sub = next(c for c in stats["conditions"] if "data TLB" in c["value"])
    assert sub["count"] == 4
    tool_events = [e for e in body.get("events", []) if e.get("name") == "compute_log_stats"]
    assert tool_events and tool_events[0]["result"]["basis"]
    assert body.get("range_coverage", {}).get("sufficient") is True


def test_missing_range_grade_event_matches_state():
    from contextlib import contextmanager
    from types import SimpleNamespace
    from app.agent.nodes import DiagnosisNodes

    @contextmanager
    def db_session():
        yield SimpleNamespace(query=lambda *a: SimpleNamespace(
            filter=lambda *a: SimpleNamespace(all=lambda: [])))

    nodes = DiagnosisNodes(SimpleNamespace(
        llm=SimpleNamespace(is_sufficient=lambda *a: True), db_session=db_session))
    result = nodes.grade({"question": "L100-L101的INFO日志多少条？", "tenant_id": "t", "hits": []})
    assert result["range_coverage"]["missing"] == [100, 101]
    assert result["docs_sufficient"] is result["events"][0]["docs_sufficient"] is False


def test_review_script_single_judge_completes_summary(tmp_path):
    """执行实际脚本函数，隔离其启动/模型配置；单判不能访问第二裁判下标。"""
    import ast
    from collections import defaultdict
    from pathlib import Path
    from types import SimpleNamespace

    script = Path(__file__).resolve().parents[1] / "eval/night20261101/run_review_fix_verify.py"
    tree = ast.parse(script.read_text(encoding="utf-8"))
    names = {"run_cross_reviews", "summarize"}
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    scope = {"SCOPE": ["q"], "JUDGE_ONLY": set(), "JUDGES": ["deepseek-v4-pro-0813"],
             "LEDGER": tmp_path / "absent.jsonl", "ROOT": tmp_path,
             "defaultdict": defaultdict, "json": json,
             "save_results": lambda records: None, "log": lambda *a: None,
             "subprocess": SimpleNamespace(run=lambda *a, **k: SimpleNamespace(stdout="test"))}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(script), "exec"), scope)
    records = {"questions": {"q": {"status": "answered"}}, "judges": {
        "q": {"deepseek-v4-pro-0813": {"verdict": {"answer_correct": True}}}}}
    scope["run_cross_reviews"](None, records, None)
    scope["summarize"](records)
    assert records["summary"]["judged_main"] == 1
    assert records["summary"]["both_judged"] == 0
    assert records["summary"]["judge_agreement_on_answer_correct"] == "0/0"
