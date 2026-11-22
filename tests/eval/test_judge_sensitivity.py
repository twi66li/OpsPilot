"""第二裁判的计分、盲判输入与单次判分约束，全部使用假裁判。"""
import json
from types import SimpleNamespace

import httpx

from eval.judge_sensitivity import Audit, compare, judge_payload, legacy_rubric, render_report, score_one


def row(identifier, kind, old, new):
    return {"id": identifier, "task_type": kind, "old": dict(zip(("answer_correct", "grounded", "abstained"), old), reason="旧理由"),
            "new": dict(zip(("answer_correct", "grounded", "abstained"), new), reason="新理由") if new else None,
            "status": "ok" if new else "error", "error": {"type": "test"}}


def test_agreement_and_scores_include_all_three_dimensions_and_error_coverage():
    rows = [row("a", "retrieval", (True, True, False), (False, True, False)),
            row("b", "retrieval", (False, True, True), (True, False, False)),
            row("c", "abstain", (True, True, True), (True, True, True)),
            row("d", "retrieval", (True, True, False), None)]
    agreement, summary = compare(rows)
    assert agreement["all_three"]["agree"] == 1
    assert agreement["all_three"]["rate"] == 1 / 3
    assert agreement["coverage"] == .75
    assert agreement["dimensions"]["answer_correct"]["disagreements"] == ["a", "b"]
    assert agreement["dimensions"]["grounded"]["rate"] == 2 / 3
    assert agreement["dimensions"]["abstained"]["disagreements"] == ["b"]
    assert summary["answer_correctness"]["old_pass"] == 2
    assert summary["answer_correctness"]["new_pass"] == 1
    assert summary["answer_correctness"]["n"] == 3
    assert summary["correct_abstention"]["new_pass"] == 1
    report = render_report({"old_judge": "old", "new_judge": "new", "frozen_unchanged": True}, rows, agreement, summary, {})
    assert "旧理由" in report and "新理由" in report and "1/3" in report
    assert "人工金标" in report and "a, b" in report


def test_rubric_is_original_literal_and_payload_does_not_reveal_old_judge():
    from pathlib import Path
    rubric = legacy_rubric(Path("eval/real_eval.py"))
    assert rubric.startswith("你是中文问答评测裁判。")
    assert "无答案题仅说明未知、边界与必要下一步即可正确" in rubric
    question = {"question": "问题", "reference_answer": "依据", "answer_points": ["要点"], "should_abstain": False}
    answer = {"answer": "回答", "judgment": {"reason": "旧裁判理由不能泄漏"}}
    payload = judge_payload(question, answer, {"hits": [{"text": "文档"}]})
    assert list(payload) == ["question", "reference_answer", "required_points", "expected_abstention", "retrieved_context", "answer"]
    assert "旧裁判" not in json.dumps(payload, ensure_ascii=False)


class FakeAPI:
    def __init__(self, responses):
        self.responses, self.calls = iter(responses), []
        self.client = SimpleNamespace(close=lambda: None)

    def chat(self, *args, **kwargs):
        response = next(self.responses)
        self.calls.append({"model": "fake", "status": "error" if isinstance(response, Exception) else "ok", "usage": {}})
        if isinstance(response, Exception):
            raise response
        return response


def inputs():
    return ({"question": "问题", "reference_answer": "参考", "answer_points": ["要点"], "should_abstain": False},
            {"id": "q1", "task_type": "retrieval", "mode": "hybrid", "answer": "冻结回答",
             "judgment": {"answer_correct": True, "grounded": True, "abstained": False, "reason": "旧理由"}},
            {"hits": [{"text": "冻结证据"}]})


def test_invalid_output_is_not_rejudged_and_secrets_are_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "private-value")
    api = FakeAPI([ValueError("private-value sk-example-secret")])
    audit = Audit(tmp_path / "audit.jsonl")
    result = score_one(*inputs(), "规则", api, audit)
    assert len(api.calls) == 1 and result["status"] == "error"
    assert "private-value" not in audit.path.read_text(encoding="utf-8")
    assert "sk-example-secret" not in json.dumps(result)
    recovered, unknown = audit.recovered()
    assert recovered["q1"]["status"] == "error" and not unknown


def test_transport_retry_is_recorded_and_completed_rows_recover_without_another_call(tmp_path, monkeypatch):
    monkeypatch.setattr("eval.judge_sensitivity.time.sleep", lambda delay: None)
    raw = '{"answer_correct":true,"grounded":true,"abstained":false,"reason":"依据充分"}'
    api = FakeAPI([httpx.ConnectError("短暂连接中断"), raw])
    audit = Audit(tmp_path / "audit.jsonl")
    result = score_one(*inputs(), "原规则", api, audit)
    assert result["status"] == "ok" and result["retries"] == 1
    recovered, unknown = audit.recovered()
    assert recovered["q1"] == result and not unknown
    assert len(api.calls) == 2


def test_in_flight_request_is_not_implicitly_reissued(tmp_path):
    audit = Audit(tmp_path / "audit.jsonl")
    audit.append({"event": "started", "id": "q1", "attempt": 1})
    recovered, unknown = audit.recovered()
    assert recovered == {} and unknown == {"q1"}
