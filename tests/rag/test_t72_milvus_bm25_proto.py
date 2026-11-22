"""T72 Milvus 2.5 原生 BM25 原型离线单测（无容器环境稳定通过，实时容器探针清晰 skip）。"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from pymilvus import DataType, FunctionType

from eval.milvus_bm25_prototype import (
    ANALYZER_CONFIGS,
    DEFAULT_REPORT,
    MilvusBM25PrototypeStore,
    build_bm25_indexes,
    build_bm25_schema,
    build_filter,
    compare_with_baseline,
    fingerprint,
    literal,
    migrate_milvus_to_bm25,
    parse_memory_to_mib,
    row_digest,
    verify_rollback_with_preserved_legacy,
)


def _mock_store(dim: int = 3) -> MilvusBM25PrototypeStore:
    store = object.__new__(MilvusBM25PrototypeStore)
    store.client = Mock()
    store.collection = "t72_proto_mock"
    store.dim = dim
    store.analyzer_params = ANALYZER_CONFIGS["chinese"]
    return store


def test_compose_proto_is_isolated_from_default_milvus():
    proto = yaml.safe_load(Path("deploy/milvus/compose.bm25-proto.yml").read_text(encoding="utf-8"))
    default = yaml.safe_load(Path("deploy/milvus/compose.yml").read_text(encoding="utf-8"))

    assert proto["name"] == "opspilot-t72-bm25"
    service = proto["services"]["milvus-bm25-proto"]
    assert service["image"].startswith("milvusdb/milvus:v2.5.")
    assert not service["image"].endswith("latest")
    assert "127.0.0.1:29530:19530" in service["ports"]
    assert "127.0.0.1:29091:9091" in service["ports"]
    assert not any(":19530:" in p or ":19531:" in p for p in service["ports"])
    assert "milvus_bm25_proto_data" in proto["volumes"]
    assert default["services"]["milvus"]["image"] == "milvusdb/milvus:v2.4.15"


def test_bm25_schema_and_indexes_define_native_function():
    schema = build_bm25_schema(dim=1024, analyzer_params=ANALYZER_CONFIGS["chinese"])
    fields = {field.name: field for field in schema.fields}

    assert fields["chunk_id"].is_primary is True
    assert fields["is_active"].dtype == DataType.BOOL
    assert fields["vector"].dtype == DataType.FLOAT_VECTOR
    assert fields["vector"].params["dim"] == 1024
    assert fields["sparse_vector"].dtype == DataType.SPARSE_FLOAT_VECTOR
    assert fields["text"].params.get("enable_analyzer") is True
    raw_analyzer = fields["text"].params.get("analyzer_params")
    assert (json.loads(raw_analyzer) if isinstance(raw_analyzer, str) else raw_analyzer) == {"type": "chinese"}

    assert len(schema.functions) == 1
    fn = schema.functions[0]
    assert fn.type == FunctionType.BM25
    assert fn.input_field_names == ["text"]
    assert fn.output_field_names == ["sparse_vector"]

    indexes = {item.field_name: item.to_dict() for item in build_bm25_indexes()}
    assert indexes["vector"]["index_type"] == "FLAT"
    assert indexes["vector"]["metric_type"] == "COSINE"
    assert indexes["sparse_vector"]["index_type"] == "SPARSE_INVERTED_INDEX"
    assert indexes["sparse_vector"]["metric_type"] == "BM25"


def test_filter_quotes_malicious_tenant_and_supports_active_version_modes():
    malicious = 'default" or tenant_id != "default'
    expr = build_filter(malicious, active_only=True, active_versions={"ops-kb-001": "v2", "ops-kb-002": None})
    assert expr == (
        f"tenant_id == {literal(malicious)} and is_active == true "
        'and ((doc_id == "ops-kb-001" and version_id == "v2") or (doc_id == "ops-kb-002" and version_id == ""))'
    )
    assert '\\"' in expr


def test_store_short_circuits_empty_active_versions_and_validates_upsert():
    store = _mock_store(dim=3)
    assert store.search_bm25("default", "ERR_5003", active_versions={}) == []
    assert store.search_dense("default", [1.0, 0.0, 0.0], active_versions={}) == []
    assert store.search_hybrid("default", "ERR_5003", [1.0, 0.0, 0.0], active_versions={}) == []
    store.client.search.assert_not_called()
    store.client.hybrid_search.assert_not_called()

    chunk = {"chunk_id": "c1", "doc_id": "d1", "doc_title": "手册", "text": "ERR_5003 排查"}
    with pytest.raises(ValueError, match="不匹配"):
        store.upsert("default", [chunk], [[1.0, 0.0]])

    store.upsert("default", [chunk], [[1.0, 0.0, 0.0]], is_active=True)
    inserted = store.client.upsert.call_args.kwargs["data"][0]
    assert "sparse_vector" not in inserted
    assert inserted["is_active"] is True
    assert inserted["text"] == "ERR_5003 排查"
    assert inserted["payload"]["tenant_id"] == "default"


def test_fingerprint_and_milvus_to_milvus_migration_guards(tmp_path: Path):
    rows = [
        {"payload": {"chunk_id": "c1", "doc_id": "d1", "doc_title": "t", "text": "ERR_5003", "tenant_id": "default"}, "vector": [1.0, 0.0, 0.0]},
        {"payload": {"chunk_id": "c2", "doc_id": "d2", "doc_title": "t", "text": "order-service", "tenant_id": "default"}, "vector": [0.0, 1.0, 0.0]},
    ]
    assert fingerprint(rows) == fingerprint(list(reversed(rows)))
    with pytest.raises(ValueError, match="重复"):
        fingerprint(rows + rows)
    mutated = {"payload": {**rows[0]["payload"], "text": "已变更"}, "vector": rows[0]["vector"]}
    assert row_digest(rows[0]) != row_digest(mutated)

    # 目标集合非空且无断点时拒绝覆盖
    client = Mock()
    it_source = Mock()
    it_source.next.side_effect = [rows, []]
    client.query_iterator.return_value = it_source
    target = _mock_store(dim=3)
    target.client = client
    client.query.return_value = [{"count(*)": 2}]
    with pytest.raises(ValueError, match="目标集合不为空"):
        migrate_milvus_to_bm25(client, "legacy_col", target, checkpoint=tmp_path / "cp.json")

    # 正常迁移 + 全量指纹校验 + 断点落盘
    client2 = Mock()
    it1, it2, it3, it4 = Mock(), Mock(), Mock(), Mock()
    it1.next.side_effect = [rows, []]
    it2.next.side_effect = [rows, []]
    it3.next.side_effect = [rows, []]
    it4.next.side_effect = [rows, []]
    client2.query_iterator.side_effect = [it1, it2, it3, it4]
    client2.query.return_value = [{"count(*)": 0}]
    client2.get.return_value = rows
    target2 = _mock_store(dim=3)
    target2.client = client2
    cp = tmp_path / "ok_cp.json"

    res = migrate_milvus_to_bm25(client2, "legacy_col", target2, checkpoint=cp)
    assert res["verified"] is True
    assert res["count_match"] is True
    assert res["sha256_match"] is True
    assert json.loads(cp.read_text(encoding="utf-8"))["complete"] is True

    # 源变化后拒绝复用断点
    bad_cp = tmp_path / "bad_cp.json"
    bad_cp.write_text(json.dumps({"signature": {"source_collection": "other"}}), encoding="utf-8")
    client3 = Mock()
    it5 = Mock()
    it5.next.side_effect = [rows, []]
    client3.query_iterator.return_value = it5
    target3 = _mock_store(dim=3)
    target3.client = client3
    with pytest.raises(ValueError, match="不能复用迁移断点"):
        migrate_milvus_to_bm25(client3, "legacy_col", target3, checkpoint=bad_cp)


def test_rollback_verification_preserves_legacy_collection():
    rows = [
        {"payload": {"chunk_id": "c1", "doc_id": "ops-kb-001", "doc_title": "手册", "text": "ERR_5003", "tenant_id": "default"}, "vector": [1.0, 0.0, 0.0]}
    ]
    expected_fp = fingerprint(rows)
    client = Mock()
    client.list_aliases.return_value = []
    client.describe_alias.side_effect = [
        {"collection_name": "legacy_col"},
        {"collection_name": "bm25_col"},
        {"collection_name": "legacy_col"},
    ]
    client.search.side_effect = [
        [[{"entity": {"payload": rows[0]["payload"]}, "distance": 0.99}]],
        [[{"entity": {"payload": rows[0]["payload"]}, "distance": 5.2}]],
        [[{"entity": {"payload": rows[0]["payload"]}, "distance": 0.99}]],
    ]
    client.has_collection.return_value = True
    it = Mock()
    it.next.side_effect = [rows, []]
    client.query_iterator.return_value = it

    result = verify_rollback_with_preserved_legacy(
        client,
        "legacy_col",
        "bm25_col",
        "active_alias",
        tenant_id="default",
        sample_query_text="ERR_5003",
        sample_query_vector=[1.0, 0.0, 0.0],
        expected_legacy_fp=expected_fp,
    )
    assert result["passed"] is True
    assert result["legacy_preserved"] is True
    assert result["legacy_fingerprint_unchanged"] is True
    assert result["dense_hits_identical_after_rollback"] is True
    client.drop_alias.assert_called_once_with(alias="active_alias")


def test_parse_memory_and_baseline_comparison():
    assert parse_memory_to_mib("512.5MiB / 7.65GiB") == 512.5
    assert parse_memory_to_mib("1.5GiB / 16GiB") == 1536.0
    assert parse_memory_to_mib("unknown") is None

    base = {"v1/test/hybrid/recall@5": 1.0, "v1/test/hybrid/mrr@5": 0.976562, "v2/test/bm25/keywords@5": 0.766667}
    proto_ok = {"v1/test/hybrid/recall@5": 1.0, "v1/test/hybrid/mrr@5": 0.984375}
    cmp_ok = compare_with_baseline(proto_ok, base)
    assert cmp_ok["all_non_regressed"] is True
    assert cmp_ok["items"]["v1/test/hybrid/mrr@5"]["delta"] == 0.007813

    proto_bad = {"v1/test/hybrid/recall@5": 0.95, "v1/test/hybrid/mrr@5": 0.976562}
    cmp_bad = compare_with_baseline(proto_bad, base)
    assert cmp_bad["all_non_regressed"] is False
    assert len(cmp_bad["regressions"]) == 1


def test_generated_prototype_report_has_required_sections():
    if not DEFAULT_REPORT.is_file():
        pytest.skip("实测报告 eval/milvus_bm25_prototype_report.json 尚未生成")
    report = json.loads(DEFAULT_REPORT.read_text(encoding="utf-8"))
    assert report["task"] == "T72"
    assert report["environment"]["milvus_image"].startswith("milvusdb/milvus:v2.5.")
    assert report["environment"]["model_calls"] == 0
    assert report["corpus_and_baseline"]["test_total_questions"] == 80
    assert report["corpus_and_baseline"]["test_retrieval_questions"] == 64

    verifications = report["verifications"]
    assert "1_chinese_tokenization" in verifications
    assert "2_exact_keyword_hits" in verifications
    assert verifications["3_tenant_filter"]["passed"] is True
    assert verifications["4_version_filter"]["passed"] is True
    assert "5_resource_usage" in verifications

    mig = report["migration_and_rollback"]
    assert mig["milvus_to_milvus_migration"]["verified"] is True
    assert mig["rollback_verification"]["passed"] is True
    assert "decision" in report["recommendation"]
    assert "summary" in report["recommendation"]


def test_live_milvus_proto_container_probe():
    """若隔离原型容器已在 127.0.0.1:29091/healthz 健康就绪则执行实时探针；无容器环境清晰 skip。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:29091/healthz", timeout=0.8) as resp:
            if resp.status != 200:
                pytest.skip("隔离 Milvus 2.5 原型容器尚未健康就绪，跳过实机探针")
    except OSError:
        pytest.skip("隔离 Milvus 2.5 原型容器未启动（127.0.0.1:29091），离线环境跳过实机探针")

    from pymilvus import MilvusClient

    client = MilvusClient(uri="http://127.0.0.1:29530", timeout=5)
    try:
        version = client.get_server_version()
        assert version.startswith("2.5") or version.startswith("v2.5")
    finally:
        client.close()
