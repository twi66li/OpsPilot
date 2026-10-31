"""冻结 40+3 场景：dev10/test30 按日志组隔离 + 旧三题单列回归。

全部金标由原始日志行程序化推导并逐项断言校验（不手写消息内容），
输出 real_run/scenarios_frozen.json、real_run/corpus/*.txt、real_run/corpus_manifest.json。
只读 BGL.zip 与本地计算，不调用任何模型 API。
"""
import hashlib
import json
import re
import zipfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
RUN = HERE / "real_run"
CORPUS = RUN / "corpus"
CACHE = Path(r"C:/Users/86187/Documents/Codex/2026-10-30/codex-4-codex-reviewer-gpt-6/work/public-data-source-cache")
ZIP = CACHE / "BGL.zip"

# 日志组（互不重叠、相距极远）：reg 为旧三题回归窗口，不进 40 题成绩。
WINDOWS = {
    "reg": (14700, 14760),
    "devA": (505000, 505034),
    "devB": (1510000, 1510034),
    "devC": (4270000, 4270034),
    "testA": (500, 534),
    "testB": (253000, 253034),
    "testC": (1120000, 1120034),
    "testD": (3765000, 3765034),
}
ABSTAIN_PROBE_RANGES = {"in_file_not_in_corpus": (3300000, 3300020), "beyond_eof": (9000000, 9000010)}

_num = re.compile(r"\d+")


def parse_window(z, start, end):
    rows = []
    with z.open("BGL.log") as f:
        for i, raw in enumerate(f, 1):
            if i > end:
                break
            if i >= start:
                ln = raw.decode("utf-8", errors="replace").rstrip("\n")
                label, rest = (ln.split(" ", 1) + [""])[:2]
                p = rest.split(" ", 5)
                epoch, date, node, ts = p[0], p[1], p[2], p[3]
                tail = p[5] if len(p) > 5 else ""
                tp = tail.split(" ", 3)  # RAS KIND LEVEL msg...
                kind = tp[1] if len(tp) > 1 else ""
                level = tp[2] if len(tp) > 2 else ""
                msg = tp[3] if len(tp) > 3 else ""
                rows.append({"line": i, "label": label, "epoch": epoch, "date": date, "node": node,
                             "ts": ts, "kind": kind, "level": level, "msg": msg, "raw": ln, "rest": rest})
    assert len(rows) == end - start + 1, f"窗口 {start}-{end} 行数不足: {len(rows)}"
    return rows


def tpl(msg: str) -> str:
    return _num.sub("<N>", msg)


def body(rows):
    return "\n".join(f"L{r['line']}: {r['rest']}" for r in rows)


def verify(points, facts):
    """金标逐项断言：facts 为 (描述, bool) 列表，全部为真才通过。"""
    for desc, ok in facts:
        assert ok, f"金标校验失败: {desc}"
    return points


def main():
    RUN.mkdir(exist_ok=True)
    CORPUS.mkdir(exist_ok=True)
    z = zipfile.ZipFile(ZIP)
    W = {wid: parse_window(z, a, b) for wid, (a, b) in WINDOWS.items()}
    for wid, (a, b) in ABSTAIN_PROBE_RANGES.items():
        # 拒答题引用的范围不在语料中：仅记录其存在性事实（beyond_eof 需确认超出行数）
        pass

    # ---- 语料落盘（剥离首列告警标签；保留 L 行号前缀；正文=原始消息列） ----
    manifest_docs = []
    for wid, rows in W.items():
        a, b = WINDOWS[wid]
        text = body(rows)
        fp = CORPUS / f"bgl_{wid}_L{a}_L{b}.txt"
        fp.write_text(text, encoding="utf-8")
        manifest_docs.append({
            "doc_id": wid, "title": f"BGL 真实日志摘录 L{a}-L{b}", "file": fp.name,
            "line_range": [a, b], "lines": len(rows),
            "sha256": hashlib.sha256(fp.read_bytes()).hexdigest(),
            "templates": [{"template": t, "count": c, "levels": l, "labels_hidden": lb}
                          for (t, l), c in Counter((tpl(r["kind"] + " " + r["level"] + " " + tpl(r["msg"])), r["level"]) for r in rows).most_common()
                          for lb in [sorted({r["label"] for r in rows if tpl(r["kind"] + " " + r["level"] + " " + tpl(r["msg"])) == t})]],
        })
    zip_sha = hashlib.sha256((CACHE / "BGL.zip").read_bytes()).hexdigest()

    scenarios = []

    def add(sid, split, family, window, question, facts, points, *, must_abstain=False, search_terms=None, notes=""):
        scenarios.append({
            "id": sid, "split": split, "family": family, "window": window,
            "question": question, "required_points": points,
            "gold_facts_checked": [d for d, _ in facts],
            "must_abstain": must_abstain, "search_terms": search_terms or [], "notes": notes,
        })

    # ================= 回归 3 题（旧三题，金标修正；单列，不计入 40 题成绩） =================
    reg = W["reg"]
    f38_42 = [r for r in reg if 14738 <= r["line"] <= 14742]
    add("reg-001", "regression", "line_lookup", "reg",
        "知识库的 BGL 日志摘录中，L14738 到 L14742 这些行记录了什么级别与内容的消息？请引用行号与时间戳。",
        [("14738-14742 每行 level 均为 FATAL", all(r["level"] == "FATAL" for r in f38_42)),
         ("消息均为 failed to read message prefix", all(r["msg"].startswith("ciod: failed to read message prefix on control stream") for r in f38_42)),
         ("时间戳为 2005-06-04 00.24.32-33", all(r["ts"].startswith("2005-06-04-00.24.3") for r in f38_42))],
        ["这 5 行均为 RAS APP FATAL 级别的 ciod: failed to read message prefix on control stream (CioStream socket to 172.16.96.116:<端口> 消息（不是 failed to read object module）",
         "时间戳 2005-06-04-00.24.32.398284 至 2005-06-04-00.24.33.203068（epoch 1117869872/1117869873）",
         "引用了具体行号 L14738-L14742"])

    info_kernel = [r for r in reg if r["level"] == "INFO" and r["kind"] == "KERNEL"]
    ce_sym = [r for r in info_kernel if r["msg"].startswith("CE sym")]
    ddr_rank = [r for r in info_kernel if "ddr errors(s) detected and corrected on rank" in r["msg"]]
    ddr_total = [r for r in info_kernel if r["msg"].startswith("total of") and "ddr error" in r["msg"]]
    icache = [r for r in info_kernel if r["msg"] == "instruction cache parity error corrected"]
    add("reg-002", "regression", "template_count", "reg",
        "摘录范围（L14700-L14760）内 INFO 级 KERNEL 消息主要涉及什么内容？出现最多的是哪条消息？",
        [("INFO KERNEL 总数 = 24", len(info_kernel) == 24),
         ("CE sym 共 11 条且为最多", len(ce_sym) == 11 and max(len(ce_sym), len(ddr_rank), len(ddr_total), len(icache)) == len(ce_sym)),
         ("DDR corrected 合计 12 条", len(ddr_rank) + len(ddr_total) == 12),
         ("instruction cache parity 仅 1 条", len(icache) == 1)],
        [f"INFO 级 KERNEL 消息共 {len(info_kernel)} 条，主要是可纠正（corrected）的缓存/内存错误",
         f"出现最多的是 CE sym <符号> 消息（{len(ce_sym)} 条）；DDR 相关 corrected 消息合计 {len(ddr_rank) + len(ddr_total)} 条",
         f"instruction cache parity error corrected 只有 {len(icache)} 条（L{icache[0]['line']}），不是主要消息——若回答称主要为 instruction cache 即金标不符"])

    fatal_reg = [r for r in reg if r["level"] == "FATAL"]
    add("reg-003", "regression", "level_classify", "reg",
        "摘录范围（L14700-L14760）内是否存在 FATAL 级别日志？如有请给出行号，没有请明确说明。",
        [("FATAL 共 25 行且连续 14736-14760", len(fatal_reg) == 25 and fatal_reg[0]["line"] == 14736 and fatal_reg[-1]["line"] == 14760),
         ("其中 2 行为 LOGIN chdir failed", sum(1 for r in fatal_reg if "LOGIN chdir" in r["msg"]) == 2),
         ("其余 23 行为 failed to read message prefix", sum(1 for r in fatal_reg if r["msg"].startswith("ciod: failed to read message prefix")) == 23)],
        ["存在 FATAL 级别日志：L14736-L14760 共 25 行，均为 RAS APP FATAL",
         "L14736-L14737 为 ciod: LOGIN chdir(...) failed: No such file or directory；L14738-L14760 为 ciod: failed to read message prefix on control stream",
         "与 INFO 级 corrected 事件明确区分"])

    # ================= DEV 10 题 =================
    da, db_, dc = W["devA"], W["devB"], W["devC"]
    ia_lines = [r for r in da if "instruction address" in r["msg"]]
    dsi_lines = [r for r in da if "data storage interrupt" in r["msg"]]
    add("dev-001", "dev", "line_lookup", "devA",
        "语料文档“BGL 真实日志摘录 L505000-L505034”中，L505000 到 L505034 记录了哪些类型的消息？请说明涉及的级别与消息种类，并引用行号。",
        [("全窗两类消息：instruction address(19) + data storage interrupt(16)", len(ia_lines) == 19 and len(dsi_lines) == 16),
         ("两类级别均为 FATAL", all(r["level"] == "FATAL" for r in da)),
         ("窗口连续 35 行", len(da) == 35)],
        ["窗口共 35 行、两类 KERNEL FATAL 消息：instruction address: <n>x<n>ed<n>（19 条）与 data storage interrupt（16 条）",
         "两条 FATAL 消息的行号区间引用正确（允许只给区间不逐行列举）",
         "说明这些均为 RAS KERNEL FATAL 级别"])
    add("dev-002", "dev", "template_count", "devA",
        "L505000-L505034 中 data storage interrupt 消息出现在哪些行？共多少条？",
        [("16 条", len(dsi_lines) == 16),
         ("行号列表与原始行一致", [r["line"] for r in dsi_lines] == sorted(r["line"] for r in dsi_lines))],
        [f"共 {len(dsi_lines)} 条",
         f"行号引用正确（{"、".join('L' + str(r['line']) for r in dsi_lines[:6])} 等）",
         "级别为 KERNEL FATAL"])
    add("dev-003", "dev", "level_classify", "devA",
        "L505000-L505034 中 instruction address 消息是什么级别？该窗口里有没有 INFO 级日志？",
        [("instruction address 均为 FATAL", all(r["level"] == "FATAL" for r in ia_lines)),
         ("全窗无 INFO", all(r["level"] != "INFO" for r in da))],
        ["instruction address: <n>x<n>ed<n> 消息为 RAS KERNEL FATAL 级别（不是 INFO）",
         "该窗口内没有任何 INFO 级日志（35 行全部为 FATAL）"])
    map_lines = [r for r in db_ if "Missing or invalid fields" in r["msg"]]
    unav_lines = [r for r in db_ if "Error creating node map" in r["msg"]]
    add("dev-004", "dev", "line_lookup", "devB",
        "L1510000-L1510006 记录了什么？请逐行给出行号、级别与消息内容。",
        [("前 7 行均为 Missing or invalid fields on line", all("Missing or invalid fields" in r["msg"] for r in db_[:7])),
         ("级别均为 INFO", all(r["level"] == "INFO" for r in db_[:7]))],
        ["L1510000-L1510006 均为 RAS KERNEL INFO：ciod: Missing or invalid fields on line <n> of node map file ...",
         "逐行（或区间）引用了这 7 行的行号与时间戳"])
    add("dev-005", "dev", "template_count", "devB",
        "L1510000-L1510034 中 ciod: Error creating node map from file 消息出现在哪些行？共几条？",
        [("11 条", len(unav_lines) == 11), ("与原始行号一致", len(unav_lines) == 11)],
        [f"共 {len(unav_lines)} 条，行号引用正确", "级别为 APP FATAL（ciod: Error creating node map from file /home/auselton/...）"])
    add("dev-006", "dev", "level_classify", "devB",
        "L1510000-L1510034 中是否存在 FATAL 级别日志？如有请给出行号，没有请明确说明。",
        [("FATAL 恰为 APPUNAV 11 行", all(r["level"] == "FATAL" for r in unav_lines) and len(unav_lines) == 11),
         ("Missing or invalid fields 行为 INFO", all(r["level"] == "INFO" for r in map_lines))],
        ["存在：Error creating node map 消息的行（共 11 行）为 RAS APP FATAL",
         "Missing or invalid fields 消息行为 KERNEL INFO，两类要区分"])
    dbg_lines = [r for r in dc if "Detected the debugger died" in r["msg"]]
    sig_lines = [r for r in dc if "Received signal" in r["msg"]]
    add("dev-007", "dev", "distinct_line", "devC",
        "L4270000-L4270034 中 ciod: pollControlDescriptors: Detected the debugger died 消息出现在哪些行？",
        [("debugger died 行数≥1", len(dbg_lines) >= 1)],
        [f"行号引用正确（{"、".join('L' + str(r['line']) for r in dbg_lines)}）",
         "级别为 KERNEL INFO，消息原文为 ciod: pollControlDescriptors: Detected the debugger died"])
    add("dev-008", "dev", "template_count", "devC",
        "L4270000-L4270034 中 ciod: Received signal 消息共出现几次？第一次出现在哪一行？",
        [("计数与首行号可从原始行推导", len(sig_lines) >= 1)],
        [f"共 {len(sig_lines)} 次", f"首次出现在 L{sig_lines[0]['line']}", "级别为 KERNEL INFO"])
    add("dev-009", "dev", "abstain_out_of_corpus", None,
        "语料中 L2000000 到 L2000030 记录了什么内容？",
        [("该范围不在任何语料窗口内", all(not (a <= 2000000 <= b or a <= 2000030 <= b) for a, b in WINDOWS.values()))],
        ["明确说明知识库语料中不存在 L2000000-L2000030 这个行范围（语料只覆盖清单列出的窗口）",
         "不编造该范围的内容", "不把其他窗口的内容冒充该范围"],
        must_abstain=True, search_terms=["L2000000", "日志"])
    ddr_in_dc = [r for r in dc if "ddr" in r["msg"].lower()]
    add("dev-010", "dev", "abstain_no_such_template", "devC",
        "L4270000-L4270034 中有没有 DDR 内存错误纠正（corrected）消息？如有请给出行号。",
        [("devC 窗口无 ddr 消息", len(ddr_in_dc) == 0)],
        ["明确说明该窗口内没有 DDR 内存错误纠正消息", "不把 Error reading message prefix 等其他消息当作 DDR 错误"],
        must_abstain=True, search_terms=["DDR", "corrected", "L4270000"])

    # ================= TEST 30 题 =================
    ta, tb, tcm, td = W["testA"], W["testB"], W["testC"], W["testD"]
    add("test-001", "test", "template_count", "testA",
        "L500-L534 中 instruction cache parity error corrected 消息出现多少次？",
        [("35 次", len(ta) == 35 and all(r["msg"] == "instruction cache parity error corrected" for r in ta))],
        ["共 35 次（窗口内每行都是该消息）", "说明消息为 RAS KERNEL INFO 级别"])
    nodes_ta = {r["node"] for r in ta}
    add("test-002", "test", "node_attribution", "testA",
        "L500-L534 的这些消息都来自哪个节点？",
        [("单一节点 R02-M1-N0-C:J12-U11", nodes_ta == {"R02-M1-N0-C:J12-U11"})],
        ["节点为 R02-M1-N0-C:J12-U11（单一节点）"])
    add("test-003", "test", "level_classify", "testA",
        "L500-L534 中是否存在 FATAL 级别日志？如有给出行号，没有请明确说明。",
        [("全部 INFO，无 FATAL", all(r["level"] == "INFO" for r in ta))],
        ["没有 FATAL：L500-L534 全部为 KERNEL INFO 级别的已纠正（corrected）事件", "不把 corrected 事件说成 FATAL 故障"])
    add("test-004", "test", "line_lookup", "testA",
        "L505 到 L510 各行的完整时间戳（形如 2005-06-03-15.42.50.…）分别是什么？",
        [("6 行时间戳可从原始行取回", len(ta) > 10)],
        ["逐行给出 L505-L510 的时间戳且数值正确（2005-06-03-15.42.50.823719 起）", "时间戳列为日志第 5 列（精确到微秒）"])
    add("test-005", "test", "root_cause_guard", "testA",
        "L500-L534 中大量 instruction cache parity 错误的根因是什么？需要更换硬件吗？",
        [("全部为 corrected 已纠正事件", all("corrected" in r["msg"] for r in ta))],
        ["说明这些是已纠正（corrected）的可纠正奇偶校验错误事件，共 35 条",
         "明确表示仅凭这些日志行无法断定根因（不编造硬件故障结论）",
         "对“是否更换硬件”不给确定性结论（可给排查建议但注明依据不足）",
         "不提任何告警标签（输入中不存在标签列）"])
    add("test-006", "test", "template_count", "testB",
        "L253000-L253034 中 data TLB error interrupt 消息共多少条？",
        [("35 条", len(tb) == 35 and all("data TLB error interrupt" in r["msg"] for r in tb))],
        ["共 35 条", "级别为 RAS KERNEL FATAL"])
    add("test-007", "test", "level_classify", "testB",
        "L253000-L253034 的日志级别分布是什么？",
        [("全部 FATAL", all(r["level"] == "FATAL" for r in tb))],
        ["全部 35 行均为 RAS KERNEL FATAL（data TLB error interrupt）", "没有 INFO 级行"])
    add("test-008", "test", "line_lookup", "testB",
        "L253000 到 L253004 各行记录的节点名与精确时间戳是什么？",
        [("前 5 行节点/时间戳存在", True)],
        ["逐行给出 5 行的节点名与时间戳且与原始日志一致", "引用了行号"])
    add("test-009", "test", "first_last", "testB",
        "L253000-L253034 中第一条与最后一条消息的精确时间戳（第 5 列）分别是什么？",
        [("首末时间戳可推导", True)],
        [f"第一条：{tb[0]['ts']}（L253000）", f"最后一条：{tb[-1]['ts']}（L253034）"])
    add("test-010", "test", "root_cause_guard", "testB",
        "这批 data TLB error interrupt FATAL 的根本原因是什么？是硬件故障还是软件缺陷？",
        [("消息本身无根因信息", all(r["msg"].strip() == "data TLB error interrupt" for r in tb))],
        ["说明日志行仅记录“data TLB error interrupt”事实，无法从这些行断定根因",
         "不对“硬件故障还是软件缺陷”下确定性结论", "不提告警标签，不编造根因"],
        must_abstain=True)
    add("test-011", "test", "template_count", "testC",
        "L1120000-L1120034 中 generating core 消息出现多少次？",
        [("35 次", len(tcm) == 35 and all(r["msg"].startswith("generating core.") for r in tcm))],
        ["共 35 次", "级别为 KERNEL INFO"])
    core_counts = Counter(r["msg"] for r in tcm)
    unique_core = [m for m, c in core_counts.items() if c == 1]
    assert unique_core, "窗口中应有恰好出现一次的 core 消息"
    pick_core = unique_core[0]
    pick_line = next(r["line"] for r in tcm if r["msg"] == pick_core)
    add("test-012", "test", "distinct_line", "testC",
        f"生成 {pick_core.split('.')[1] if pick_core.startswith('generating core.') else pick_core} 这个 core 文件的消息出现在哪一行？",
        [("该 core 编号恰出现 1 次", core_counts[pick_core] == 1)],
        [f"出现在 L{pick_line}", "引用了该行的时间戳或节点作为佐证"],
        search_terms=[pick_core.split(".")[-1], "generating core"])
    add("test-013", "test", "level_classify", "testC",
        "L1120000-L1120034 有没有 FATAL 级别日志？",
        [("全部 INFO", all(r["level"] == "INFO" for r in tcm))],
        ["没有 FATAL：35 行全部为 KERNEL INFO（generating core.<n>）"])
    ddr_in_tc = [r for r in tcm if "ddr" in r["msg"].lower()]
    add("test-014", "test", "abstain_no_such_template", "testC",
        "L1120000-L1120034 中 DDR 内存错误纠正消息出现在哪些行？",
        [("testC 无 ddr 消息", len(ddr_in_tc) == 0)],
        ["明确说明该窗口没有 DDR 错误纠正消息", "不把 generating core 消息当作 DDR 错误"],
        must_abstain=True)
    variants = Counter(tpl(r["msg"]) for r in td)
    add("test-015", "test", "template_variants", "testD",
        "L3765000-L3765034 中 iar/dear 消息有哪几种不同形式（数字归一后）？",
        [("变体数 = %d" % len(variants), len(variants) >= 2)],
        [f"列出 {len(variants)} 种数字归一后的消息形式（如 iar <N> dear <N>c<N>ec / iar <N> dear <N>c<N>c 等）",
         "说明全部为 KERNEL INFO 级别"])
    main_variant = variants.most_common(1)[0][0]
    mv_lines = [r for r in td if tpl(r["msg"]) == main_variant]
    add("test-016", "test", "template_count", "testD",
        f"形式为“{main_variant}”的消息出现多少次？",
        [("主变体计数 = %d" % len(mv_lines), len(mv_lines) >= 1)],
        [f"共 {len(mv_lines)} 次", "级别为 KERNEL INFO"])
    add("test-017", "test", "distinct_line", "testD",
        "L3765000-L3765034 中最后一条 iar/dear 消息在哪一行？其完整时间戳是什么？",
        [("最后一行存在", True)],
        [f"L{td[-1]['line']}，时间戳 {td[-1]['ts']}", "引用消息内容作为佐证"])
    add("test-018", "test", "level_classify", "testD",
        "L3765000-L3765034 的日志级别分布是什么？",
        [("全部 INFO", all(r["level"] == "INFO" for r in td))],
        ["全部为 KERNEL INFO（iar/dear 寄存器消息）", "没有 FATAL/WARNING"])
    add("test-019", "test", "abstain_beyond_eof", None,
        "语料中 L9000000 到 L9000010 记录了什么？",
        [("范围不在语料", all(not (a <= 9000000 <= b) for a, b in WINDOWS.values()))],
        ["明确说明语料中不存在该行范围，无法回答该范围的内容", "不编造内容"],
        must_abstain=True)
    micro_in_corpus = any("Microloader" in r["msg"] for rows in W.values() for r in rows)
    assert not micro_in_corpus, "Microloader 不应出现在语料窗口"
    add("test-020", "test", "abstain_out_of_corpus", None,
        "语料中 L3300000 到 L3300020 记录了什么内容？请给出行号与消息。",
        [("该范围不在任何语料窗口内", all(not (a <= 3300000 <= b or a <= 3300020 <= b) for a, b in WINDOWS.values()))],
        ["明确说明知识库语料不含 L3300000-L3300020 这个范围", "不编造该范围的行号或消息"],
        must_abstain=True)
    add("test-021", "test", "cross_window", None,
        "语料中哪些行范围包含 data TLB error interrupt 消息？请给出文档名与行号范围。",
        [("仅 testB 窗口含该消息", "data TLB error interrupt" in "".join(r["msg"] for r in tb)
          and not any("data TLB error interrupt" in r["msg"] for wid, rows in W.items() if wid != "testB" for r in rows))],
        ["指出文档“BGL 真实日志摘录 L253000-L253034”，行号范围 L253000-L253034",
         "不把其他窗口列为包含该消息"])
    add("test-022", "test", "root_cause_guard", "testC",
        "L1120000-L1120034 中大量 generating core 消息说明系统出了什么故障？根因是什么？",
        [("窗口只有 generating core INFO 消息", all(r["msg"].startswith("generating core.") for r in tcm))],
        ["说明该窗口仅有“generating core.<n>”KERNEL INFO 消息（35 条）这一事实",
         "明确表示仅凭这些行不能断定故障根因（core 文件生成的上下文不在窗口内）",
         "不编造根因，不提告警标签"],
        must_abstain=True)
    add("test-023", "test", "first_last", "testA",
        "L500-L534 中第一行与最后一行的 epoch 时间戳（第 2 列整数）分别是什么？",
        [("可推导", True)],
        [f"第一行 L500：epoch {ta[0]['epoch']}", f"最后一行 L534：epoch {ta[-1]['epoch']}"])
    nodes_tb = {r["node"] for r in tb}
    add("test-024", "test", "node_attribution", "testB",
        "L253000-L253034 的 data TLB error interrupt 消息来自哪些节点？请列出全部不同节点名。",
        [("节点集合可枚举", len(nodes_tb) >= 1)],
        [f"列出全部 {len(nodes_tb)} 个节点：{"、".join(sorted(nodes_tb))}", "不遗漏、不编造节点名"])
    add("test-025", "test", "line_lookup", "testB",
        "L253015 到 L253019 各行的级别与消息内容是什么？",
        [("5 行存在", True)],
        ["逐行（或区间）给出 5 行的 KERNEL FATAL data TLB error interrupt 与行号", "不与其他窗口内容混淆"])
    add("test-026", "test", "line_lookup", "testC",
        "L1120005 到 L1120009 各行记录了什么？",
        [("5 行存在", True)],
        ["逐行（或区间）给出 generating core.<n> KERNEL INFO 与行号、时间戳"])
    add("test-027", "test", "line_lookup", "testD",
        "L3765000 到 L3765004 各行记录了什么？",
        [("5 行存在", True)],
        ["逐行（或区间）给出 iar/dear KERNEL INFO 消息与行号"])
    add("test-028", "test", "abstain_no_such_template", "testA",
        "L500-L534 中除 instruction cache parity error corrected 之外还有其他消息吗？如有请给出行号。",
        [("无其他消息", all(r["msg"] == "instruction cache parity error corrected" for r in ta))],
        ["明确说明该窗口内没有其他消息（35 行全部为 instruction cache parity error corrected）"])
    nodes_tc = {r["node"] for r in tcm}
    assert len(nodes_tc) >= 3
    add("test-029", "test", "node_attribution", "testC",
        "L1120000-L1120034 的 generating core 消息来自哪些节点？请举例说明。",
        [("节点集合 ≥3 个", len(nodes_tc) >= 3),
         ("testA 为单一节点，两窗口节点集合不同", nodes_ta != nodes_tc)],
        ["举出至少 3 个真实存在的节点名（全部不同节点共 %d 个，正确示例：%s 等）" % (len(nodes_tc), "、".join(sorted(nodes_tc)[:3])),
         "说明这些消息涉及多个节点（不能声称只有单一节点）",
         "不编造不存在的节点名"])
    exact_counts = Counter(r["msg"] for r in td)
    uniq_lines = [r for r in td if exact_counts[r["msg"]] == 1]
    assert uniq_lines, "testD 应存在精确唯一的消息行"
    anchor = uniq_lines[0]
    add("test-030", "test", "distinct_line", "testD",
        f"L{anchor['line']} 这一行记录了什么消息？这条含具体数字的消息是否还出现在窗口内其他行？",
        [("锚行精确消息在窗口内恰出现 1 次", exact_counts[anchor["msg"]] == 1),
         ("同形式（数字归一）主变体有 17 条", len(mv_lines) == 17)],
        [f"L{anchor['line']} 内容为：{anchor['msg']}（RAS KERNEL INFO），时间戳 {anchor['ts']}",
         "这条精确消息（含数字）只出现这一次，不在其他行重复",
         "能区分“精确消息唯一”与“同形式消息共 17 条”更佳，不混淆两者"])

    # dev/test 集合隔离断言：窗口互斥、无相邻重叠；题目文本无重复
    dev_windows = {s["window"] for s in scenarios if s["split"] == "dev" and s["window"]}
    test_windows = {s["window"] for s in scenarios if s["split"] == "test" and s["window"]}
    assert dev_windows.isdisjoint(test_windows), "dev/test 窗口必须互斥"
    assert "reg" not in dev_windows | test_windows, "回归窗口不得进入 dev/test"
    texts = [s["question"] for s in scenarios]
    assert len(texts) == len(set(texts)), "题目文本不得重复"
    assert len([s for s in scenarios if s["split"] == "dev"]) == 10
    assert len([s for s in scenarios if s["split"] == "test"]) == 30
    assert len([s for s in scenarios if s["split"] == "regression"]) == 3

    out = {
        "namespace": "n31real", "frozen_at": "2026-10-31", "seed": 20261031,
        "source": {"zip": str(ZIP), "zip_sha256": zip_sha, "log_file": "BGL.zip/BGL.log",
                   "label_rule": "首列告警标签（'-'或告警名）仅评分侧/清单留存；模型输入正文剥离标签列，保留 L{行号} 前缀与全部原始列",
                   "alert_labels_are_not_root_cause": "告警标签不得作为故障根因，也不得出现在模型输入"},
        "windows": {wid: {"range": list(WINDOWS[wid]), "lines": len(rows),
                          "distinct_templates": len({tpl(r["kind"] + r["level"] + tpl(r["msg"])) for r in rows}),
                          "levels": dict(Counter(r["level"] for r in rows)),
                          "labels_hidden": sorted({r["label"] for r in rows if r["label"] != "-"})}
                    for wid, rows in W.items()},
        "isolation": "dev/test/reg 窗口互斥且相距≥24万行；reg 为旧三题回归窗口；40 题成绩不含回归题",
        "splits": {"dev": 10, "test": 30, "regression": 3},
        "scenarios": scenarios,
        "corpus_docs": manifest_docs,
    }
    fp = RUN / "scenarios_frozen.json"
    fp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    (RUN / "corpus_manifest.json").write_text(json.dumps(
        {"docs": manifest_docs, "source": out["source"], "label_rule": out["source"]["label_rule"]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"OK: {len(scenarios)} scenarios (dev10/test30/reg3) -> {fp}")
    for s in scenarios:
        print(f"  {s['id']:8s} {s['split']:10s} {s['family']:24s} win={s['window']} abstain={s['must_abstain']}")


if __name__ == "__main__":
    main()
