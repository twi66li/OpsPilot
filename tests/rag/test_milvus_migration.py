"""真实 Qdrant 内存 SDK 为源，覆盖试迁移的断点与完整性保护。"""

import json
from unittest.mock import Mock

import pytest

from app.rag.vector_store import QdrantVectorStore
from scripts.migrate_milvus import fingerprint, migrate, row_digest, source_rows


@pytest.fixture
def source():
    store = QdrantVectorStore(mode="memory", dim=3)
    store.upsert("alice", [{"chunk_id": "c1", "doc_id": "d1", "doc_title": "测试手册", "text": "固定资料"}],
                 [[1.0, 0.0, 0.0]])
    yield store
    store.client.close()


def test_source_export_keeps_vectors_payload_and_order_independent_digest(source):
    rows = list(source_rows(source.client, source.collection))
    assert rows[0]["vector"] == [1.0, 0.0, 0.0]
    assert rows[0]["payload"]["tenant_id"] == "alice"
    assert fingerprint(rows) == fingerprint(list(reversed(rows)))
    with pytest.raises(ValueError, match="重复"):
        fingerprint(rows + rows)
    changed = {"payload": {**rows[0]["payload"], "text": "内容已变"}, "vector": rows[0]["vector"]}
    assert row_digest(rows[0]) != row_digest(changed)


def test_no_checkpoint_rejects_existing_destination(source, tmp_path):
    target = Mock(collection="existing", dim=3)
    target.count.return_value = 2
    with pytest.raises(ValueError, match="目标不为空"):
        migrate(source.client, source.collection, target, tmp_path / "checkpoint.json", "target1")
    target.upsert.assert_not_called()


def test_changed_source_or_destination_cannot_reuse_checkpoint(source, tmp_path):
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps({"signature": {"source": "another-source"}}), encoding="utf-8")
    target = Mock(collection="target", dim=3)
    with pytest.raises(ValueError, match="不能复用断点"):
        migrate(source.client, source.collection, target, checkpoint, "target1")
    target.upsert.assert_not_called()


def test_failed_flush_does_not_mark_migration_complete_and_can_resume(source, tmp_path):
    rows = list(source_rows(source.client, source.collection))
    checkpoint = tmp_path / "checkpoint.json"
    target = Mock(collection="target", dim=3)
    target.count.return_value = 0
    target.client.get.return_value = rows
    target.client.flush.side_effect = ConnectionError("lost connection before final verification")
    with pytest.raises(ConnectionError):
        migrate(source.client, source.collection, target, checkpoint, "target1")
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["complete"] is False
    target.client.flush.side_effect = None
    target.client.query_iterator.return_value.next.side_effect = [rows, []]
    result = migrate(source.client, source.collection, target, checkpoint, "target1")
    assert result["verified"] is True
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["copied"] == 1
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["complete"] is True
