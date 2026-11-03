import json
from pathlib import Path

import httpx
import pytest
from openai import APIStatusError

from app.services.model_api import ChatAPI


@pytest.mark.parametrize("code,status,switch", [
    ("AllocationQuota.FreeTierOnly", 403, True),
    ("AccessDenied", 403, False),
    ("Unauthorized", 401, False),
])
def test_provider_quota_switch_keeps_other_errors(monkeypatch, code, status, switch):
    monkeypatch.setenv("CHAT_MODEL_CANDIDATES", json.dumps(["backup"]))
    api = ChatAPI.__new__(ChatAPI)
    api.model, api.calls = "primary", []
    attempted = []

    def recorded(model, *args, **kwargs):
        attempted.append(model)
        if model == "primary":
            raise APIStatusError(code, response=httpx.Response(status, request=httpx.Request("POST", "http://recorded")),
                                 body={"code": code})
        return "recorded backup"

    api._chat_once = recorded
    if switch:
        assert api.chat("system", "question") == "recorded backup"
        assert attempted == ["primary", "backup"]
        assert api.calls[0]["reason"] == code
    else:
        with pytest.raises(APIStatusError):
            api.chat("system", "question")
        assert attempted == ["primary"]


def test_missing_window_gold_matches_its_own_source():
    run = Path(__file__).resolve().parents[1] / "eval/night20261102q"
    for frozen in (run / "scenarios_frozen.json", run / "quality_run/scenarios_frozen.json"):
        data = json.loads(frozen.read_text(encoding="utf-8"))
        question = next(q for q in data["questions"] if q["id"] == "heldout-q6")
        doc = next(d for d in data["corpus_manifest"] if d["start"] == question["window_start"])
        raw = (run / "corpus" / doc["file"]).read_text(encoding="utf-8").splitlines()
        count = sum("Node card is not fully functional" in line for line in raw)
        assert question["gold_program"]["covered_count"] == count == 11
        assert len(question["gold_program"]["asked_range"]["missing"]) == 15
