import json

import pytest

from nomadicos.vector.base import format_version_header
from nomadicos.vector.exact import ExactVectorStore


@pytest.fixture()
def store() -> ExactVectorStore:
    s = ExactVectorStore(
        dimensions=4, metric="cosine", embedding_model="fake-embed", embedding_version="1"
    )
    s.upsert("a", [1, 0, 0, 0], {"kind": "fact"})
    s.upsert("b", [0.9, 0.1, 0, 0], {"kind": "fact"})
    s.upsert("c", [0, 1, 0, 0], {"kind": "doc"})
    return s


def test_exact_search_ranks_by_cosine(store: ExactVectorStore) -> None:
    results = store.search([1, 0, 0, 0], top_k=2)
    assert results[0].vector_id == "a"
    assert results[0].score == 1.0
    assert results[1].vector_id == "b"
    assert set(store.stats()) >= {"count", "dimensions", "metric", "format_version"}


def test_metadata_filtering(store: ExactVectorStore) -> None:
    results = store.search([1, 0, 0, 0], top_k=5, filters={"kind": "doc"})
    assert [m.vector_id for m in results] == ["c"]


def test_upsert_get_delete_roundtrip(store: ExactVectorStore) -> None:
    pair = store.get("a")
    assert pair is not None and pair[1] == {"kind": "fact"}
    assert store.delete("a") is True
    assert store.get("a") is None
    assert store.delete("a") is False
    assert store.count() == 2


def test_validation_rejects_bad_vectors(store: ExactVectorStore) -> None:
    with pytest.raises(ValueError, match="dimension"):
        store.upsert("x", [1, 0], {})
    with pytest.raises(ValueError, match="finite"):
        store.upsert("x", [1, 0, float("nan"), 0], {})
    with pytest.raises(ValueError, match="numbers"):
        store.upsert("x", [1, 0, 0, "high"], {})
    with pytest.raises(ValueError):
        store.search([1, 0, 0, 0], top_k=0)


def test_snapshot_restore_roundtrip(store: ExactVectorStore, tmp_path) -> None:
    path = store.snapshot(tmp_path / "idx")
    assert path.exists()
    header = json.loads(path.read_text(encoding="utf-8"))["header"]
    assert header["format_version"] == "0.1"
    assert header["embedding_model"] == "fake-embed"

    restored = ExactVectorStore.restore(tmp_path / "idx")
    assert restored.count() == 3
    assert restored.search([1, 0, 0, 0], top_k=1)[0].vector_id == "a"


def test_restore_rejects_unknown_format_version(tmp_path) -> None:
    import json

    (tmp_path / "index.json").write_text(
        json.dumps(
            {"header": {"format_version": "9.9"}, "vectors": {}, "metadata": {}, "order": []}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="format version mismatch"):
        ExactVectorStore.restore(tmp_path)


def test_format_header_validates_metric() -> None:
    with pytest.raises(ValueError, match="metric"):
        format_version_header(
            dimensions=384, metric="euclidean-ish", embedding_model="m", embedding_version="1"
        )
