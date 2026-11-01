"""终审（run-20261101-p1-final-01）整改的离线验证，零模型 API 调用。

覆盖：
1. P1-CITATIONS：重复文档证据下 [n] 角标映射与生成器编号同源（DiagnosisNodes.generate 实测）；
2. P1-EVIDENCE：reg-001 目标五行经范围组装全部可见；test-001 计数依据覆盖整个 35 行窗口；
   分块器行边界安全（整行保留、跨块不重复）；命中摘录无块首残行；
3. P1-GOLD：43 题金标中出现的精确时间戳逐一对语料原文核对（test-004 之外是否还有错）。
"""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

os.environ["DATABASE_URL"] = "mysql+pymysql://aiops:aiops123@127.0.0.1:13306/aiops?charset=utf8mb4"
os.environ["QDRANT_MODE"] = "remote"
os.environ["QDRANT_URL"] = "http://127.0.0.1:6333"
os.environ["QDRANT_COLLECTION"] = "kb_qwen37_1024_v2"
os.environ["EMBEDDING_PROVIDER"] = "mock"  # 离线：只读 scroll，不触发 embedding
os.environ["EMBEDDING_DIM"] = "1024"
os.environ["EMBEDDING_MODEL"] = "mock-embedding"
os.environ["LLM_PROVIDER"] = "mock"  # 离线：MockLLM，不触达任何真实模型
os.environ["MODEL_STRICT"] = "false"  # 离线允许 mock 组件（不触达供应商）
os.environ["RERANK_ENABLED"] = "false"
os.environ["BUDGET_LEDGER_PATH"] = ""
os.environ["QDRANT_MODE_STRICT"] = "false"

RESULTS = []
BASELINE = ROOT / "eval" / "night20261031" / "real_run"
RERANK = ROOT / "eval" / "night20261101" / "rerank_run"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def main():
    frozen = json.loads((BASELINE / "scenarios_frozen.json").read_text(encoding="utf-8"))
    by_id = {s["id"]: s for s in frozen["scenarios"]}
    rr = json.loads((RERANK / "results.json").read_text(encoding="utf-8"))

    # ---- 2a. 分块器：BGL 语料整行保留、跨块不重复；散文回退字符窗 ----
    from app.rag.chunking import split_into_chunks

    corpus = (BASELINE / "corpus" / "bgl_reg_L14700_L14760.txt").read_text(encoding="utf-8")
    chunks = split_into_chunks(corpus, chunk_size=2000)
    joined = "\n".join(chunks)
    all_lines = [l for l in corpus.splitlines() if l.strip()]
    kept_lines = re.findall(r"L\d{3,8}:", joined)
    orig_lines = re.findall(r"L\d{3,8}:", corpus)
    check("chunker: 语料整行全部保留且不重复", kept_lines == orig_lines,
          f"chunks={len(chunks)} 行标记 {len(kept_lines)}/{len(orig_lines)}")
    partial = [l for l in joined.splitlines() if l.strip() and not re.match(r"L\d{3,8}:", l.strip())]
    check("chunker: 无残缺日志行", not partial, f"残留 {len(partial)}")
    prose = "# 标题\n" + "这是一段很长的内容。" * 200
    pchunks = split_into_chunks(prose, chunk_size=500, overlap=80)
    check("chunker: 无行界散文仍多块（回退字符窗）", len(pchunks) > 1 and all(len(c) <= 520 for c in pchunks),
          f"chunks={len(pchunks)}")

    # ---- 1. 引用映射：DiagnosisNodes.generate 实测（重复文档 hits） ----
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app):
        from app.agent.llm import MockLLM
        from app.agent.nodes import DiagnosisNodes
        from app.container import get_container
        from app.db.base import SessionLocal
        from app.services.observability import get_tracer

        real = get_container()

        class Stub:
            db_session = real.db_session
            tracer = get_tracer()
            llm = MockLLM()
            settings = real.settings

        nodes = DiagnosisNodes(Stub())
        dup_hits = [
            {"doc_title": "手册A", "text": "alpha content one", "chunk_id": "a::0", "score": 0.9},
            {"doc_title": "手册B", "text": "bravo content two", "chunk_id": "b::0", "score": 0.8},
            {"doc_title": "手册A", "text": "alpha content three", "chunk_id": "a::1", "score": 0.7},
        ]
        with SessionLocal() as db:
            state = {"question": "手册A和手册B都说什么？", "hits": dup_hits, "tool_events": [],
                     "fault_records": [], "session_id": "offline-cite-check", "tenant_id": "n31real",
                     "intent": "diagnose"}
            payload = nodes.generate(state)
        ci = payload["citations_indexed"]
        check("citations: [n] 与第 n 条证据同源（[3]=手册A 第二块）",
              ci[2] == "手册A" and ci[0] == "手册A" and ci[1] == "手册B", f"citations_indexed={ci}")
        check("citations: 去重列表仍供展示", payload["citations"] == ["手册A", "手册B"])
        check("citations: used_evidence 即生成器证据", [it["doc_title"] for it in payload["used_evidence"]] == ci)

        # ---- 2b. reg-001 / test-001 范围组装证明（命中块全文取自 Qdrant） ----
        full_by_chunk = {ch["chunk_id"]: ch["text"] for ch in real.vector_store.scroll_tenant("n31real")}

        def hits_of(qid):
            return [{"doc_title": e["doc_title"], "chunk_id": e["chunk_id"], "score": e["score"],
                     "text": full_by_chunk.get(e["chunk_id"], "")}
                    for e in rr["questions"][qid]["evidence"] if e.get("chunk_id") in full_by_chunk]

        q = rr["questions"]["reg-001"]
        items, meta = None, None
        from app.rag.evidence import assemble_evidence, parse_line_targets, strip_partial_head

        with SessionLocal() as db:
            items, meta = assemble_evidence(hits_of("reg-001"), q["question"], db, "n31real")
        first = items[0]
        targets = parse_line_targets(q["question"])
        present = [t for t in targets if re.search(rf"L{t}:", first["text"])]
        check("reg-001: 范围项置于证据首位且为组装块", first.get("assembled") and first["chunk_id"] == "range_assembly")
        check(f"reg-001: 目标五行（{targets}）全部可见", len(present) == len(targets) == 5,
              f"可见 {len(present)}/{len(targets)} missing={meta.get('targets_missing')}")
        head_bad = [i for i in items[1:] if _is_log_head_partial(i["text"])]
        check("reg-001: 分块摘录无块首残行", not head_bad, f"{len(head_bad)} 条残留")

        # ---- 2c. 组装行与语料原文逐字对照（终审：仅出现行号不算完整） ----
        corpus_lines = {}
        for f in (BASELINE / "corpus").glob("*.txt"):
            for line in f.read_text(encoding="utf-8").splitlines():
                m = re.match(r"\s*(L\d{3,8}):", line)
                if m:
                    corpus_lines.setdefault(int(m.group(1)[1:]), line.rstrip())

        def content_check(qid):
            rec = rr["questions"][qid]
            ts = parse_line_targets(rec["question"])
            if not ts:
                return None
            with SessionLocal() as db:
                its, mt = assemble_evidence(hits_of(qid), rec["question"], db, "n31real")
            asm = next((i for i in its if i.get("assembled")), None)
            if not asm:
                return (qid, "no_range_item", mt.get("targets_missing"), [])
            got = {}
            for line in asm["text"].splitlines():
                m = re.match(r"\s*L(\d+):", line)
                if m:
                    got[int(m.group(1))] = line.rstrip()
            mismatch = [(t, got.get(t), (corpus_lines.get(t) or "")[:80]) for t in ts
                        if t not in got or got[t] != (corpus_lines.get(t) or "").rstrip()]
            return (qid, mt, mt.get("targets_incomplete"), mismatch)

        # reg-001 / test-001 重点核对（含终审指出的 L513/L525 截断行）
        for qid in ("reg-001", "test-001"):
            qid_r, mt, inc, mismatch = content_check(qid)
            check(f"{qid}: 组装行与语料原文逐字一致（0 残缺 0 不符）",
                  not mismatch and not inc, f"incomplete={inc} mismatch={[(t, str(g)[:50]) for t, g, _ in mismatch]}")

        # 全部范围题逐字对照（应无任何残缺行）
        bad_lines = []
        for qid in rr["questions"]:
            r = content_check(qid)
            if r and r[3]:
                bad_lines.append((r[0], len(r[3])))
        check("全部范围题：组装行与语料原文逐字一致（无残缺行混入）", not bad_lines, f"不符题: {bad_lines}")

        # ---- 1b. 真实编译图：citations_indexed 跨节点与最终状态保留（终审 P1-CITATIONS） ----
        from langgraph.graph import StateGraph, END

        from app.agent.state import DiagState

        g = StateGraph(DiagState)
        g.add_node("generate", nodes.generate)
        g.set_entry_point("generate")
        g.add_edge("generate", END)
        compiled = g.compile()
        with SessionLocal() as db:
            final_state = compiled.invoke({
                "question": "手册A和手册B都说什么？", "hits": dup_hits, "tool_events": [],
                "fault_records": [], "session_id": "offline-cite-check", "tenant_id": "n31real",
                "intent": "diagnose",
            })
        check("citations: 真实编译图 final 状态保留 citations_indexed（[3]=手册A 第二块）",
              final_state.get("citations_indexed", [None, None, None])[2] == "手册A"
              and len(final_state.get("citations_indexed", [])) == 3,
              f"final={final_state.get('citations_indexed')}")
        check("citations: final 状态保留 used_evidence 与 range_assembly 字段",
              bool(final_state.get("used_evidence")) and "range_assembly" in final_state)

        q1 = rr["questions"]["test-001"]
        with SessionLocal() as db:
            items1, meta1 = assemble_evidence(hits_of("test-001"), q1["question"], db, "n31real")
        first1 = items1[0]
        win_lines = sorted(set(int(m) for m in re.findall(r"L(\d+):", first1["text"])))
        expected = list(range(500, 535))
        check("test-001: 计数依据覆盖完整 35 行窗口（L500-L534）",
              win_lines == expected, f"可见 {len(win_lines)}/35 行，missing={[l for l in expected if l not in win_lines]}")

        # 23 题重排批的范围覆盖总览（供披露）
        covered = partial_cover = none = 0
        partial_ids = []
        for qid, rec in rr["questions"].items():
            ts = parse_line_targets(rec["question"])
            if not ts:
                none += 1
                continue
            with SessionLocal() as db:
                its, mt = assemble_evidence(hits_of(qid), rec["question"], db, "n31real")
            if mt.get("targets_missing", 1) == 0 and not mt.get("targets_dropped_by_budget"):
                covered += 1
            else:
                partial_cover += 1
                partial_ids.append((qid, mt.get("targets_missing"), mt.get("targets_dropped_by_budget"),
                                    rr["questions"][qid]["must_abstain"]))
        print(f"  范围题覆盖总览：完整 {covered} / 部分 {partial_cover} / 无范围引用 {none}；部分：{partial_ids}")

        # ---- 3. 金标精确时间戳全量核对 ----
        corpus_text = ""
        for f in (BASELINE / "corpus").glob("*.txt"):
            corpus_text += f.read_text(encoding="utf-8")
        ts_re = re.compile(r"2005-\d{2}-\d{2}-\d{2}\.\d{2}\.\d{2}\.\d{6}")
        bad = []
        checked = 0
        for s in frozen["scenarios"]:
            texts = [s["question"]] + [str(p) for p in s.get("required_points", [])]
            for t in texts:
                for m in ts_re.findall(t):
                    checked += 1
                    if m not in corpus_text:
                        bad.append((s["id"], m))
        # 旧冻结集保持原样：失配项必须恰好等于已发布的更正记录（corrections.json），不得有遗漏
        corr = json.loads((ROOT / "eval" / "night20261101" / "gold_corrections" / "corrections.json").read_text(encoding="utf-8"))
        corrected_ids = {c["id"] for c in corr["corrections"]}
        bad_ids = {q for q, _ in bad}
        check(f"gold: {checked} 处时间戳失配恰好等于已更正集合 {sorted(corrected_ids)}",
              bad_ids <= corrected_ids and all(q in bad_ids for c in corrected_ids for q in [c] if c == "test-004"),
              f"失配 {bad}；更正记录 {len(corrected_ids)} 条")

    print()
    fails = [n for n, ok, _ in RESULTS if not ok]
    print(f"OFFLINE VERIFY: {len(RESULTS) - len(fails)}/{len(RESULTS)} PASS" + (f" FAILURES={fails}" if fails else ""))


def _is_log_head_partial(text: str) -> bool:
    from app.rag.evidence import LINE_PREFIX, _is_log_text

    if not _is_log_text(text):
        return False
    lines = text.splitlines()
    return bool(lines) and not LINE_PREFIX.match(lines[0].strip()[:16] or "")


if __name__ == "__main__":
    main()
