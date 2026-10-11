"""Pure lexical reuse must stay bounded and end with its write transaction."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from commontrace import fact_conflicts, fact_store
from commontrace import hierarchical as h


def features(text):
    signature = h._signature(text)
    return h._normalize_statement(text), signature, h._content(signature[0]), fact_conflicts.slot_key(text)


def test_cached_features_preserve_lexical_values_and_reuse_immutable_signature(tmp_path):
    texts = ["  Vault does NOT retain 7.5 replicas  ", "Operator's journal stores 12,4 records", "部署 uses durable storage"]
    expected = [features(text) for text in texts]
    root = str(tmp_path)
    fact_store.migrate(root)
    with fact_store.transaction(root, write=True):
        owner = h._STATEMENT_FEATURES.get()
        assert owner is not None
        assert [features(text) for text in texts] == expected
        for text in texts:
            assert h._signature(text) is h._signature(text)
            assert fact_conflicts.slot_key(text) is fact_conflicts.slot_key(text)
        assert owner.values
        assert all(type(value) in (str, tuple, frozenset) for value in owner.values.values())
    assert h._STATEMENT_FEATURES.get() is None


@pytest.mark.parametrize("abort", [False, True])
def test_feature_owner_physically_clears_on_commit_and_abort(tmp_path, abort):
    root = str(tmp_path)
    fact_store.migrate(root)
    owner = None
    try:
        with fact_store.transaction(root, write=True):
            owner = h._STATEMENT_FEATURES.get()
            assert owner is not None
            features("Private lexical owner retains no completed-request entries")
            assert owner.values and owner.weight > 0
            if abort:
                raise RuntimeError("abort")
    except RuntimeError as exc:
        assert abort and str(exc) == "abort"
    assert owner is not None and owner.closed and owner.values == {} and owner.weight == 0
    assert h._STATEMENT_FEATURES.get() is None
    with fact_store.transaction(root):
        assert h._STATEMENT_FEATURES.get() is None


@pytest.mark.parametrize("budget", [1024, 10_000])
def test_feature_cache_enforces_entry_and_weight_bounds_and_closed_lifetime(budget):
    owner = h._StatementFeatures(max_entries=3, max_weight=budget)
    for n in range(100):
        text = "NORMALIZED " + str(n)
        assert owner.get("norm", text, str.lower) == text.lower()
        assert len(owner.values) <= 3 and owner.weight <= budget
    assert len(owner.values) < 100
    owner.close()
    assert owner.get("norm", "AFTER CLOSE", str.lower) == "after close"
    assert owner.closed and owner.values == {} and owner.weight == 0


def test_oversized_and_mutable_values_are_computed_without_retention():
    tiny = h._StatementFeatures(max_entries=3, max_weight=1)
    assert tiny.get("norm", "TOO LARGE", str.lower) == "too large"
    assert tiny.values == {} and tiny.weight == 0
    owner = h._StatementFeatures()
    first = owner.get("custom", "key", lambda text: [text])
    second = owner.get("custom", "key", lambda text: [text])
    assert first == second and first is not second and owner.values == {}


def test_feature_owner_clears_when_final_projection_fails(tmp_path, monkeypatch):
    root = str(tmp_path)
    fact_store.migrate(root)

    def fail_flush(self):
        raise RuntimeError("projection failed")

    monkeypatch.setattr(fact_store.FactMap, "flush", fail_flush)
    with pytest.raises(RuntimeError, match="projection failed"):
        with fact_store.transaction(root, write=True):
            owner = h._STATEMENT_FEATURES.get()
            features("A failed final projection releases lexical values too")
            assert owner.values
    assert owner.closed and owner.values == {} and owner.weight == 0
    assert h._STATEMENT_FEATURES.get() is None


def test_nested_write_owners_restore_parent_without_sharing_entries(tmp_path):
    roots = [str(tmp_path / name) for name in ("outer", "inner")]
    for root in roots:
        fact_store.migrate(root)
    with fact_store.transaction(roots[0], write=True):
        outer = h._STATEMENT_FEATURES.get()
        original = h._signature("Outer retains a pure immutable signature")
        with fact_store.transaction(roots[1], write=True):
            inner = h._STATEMENT_FEATURES.get()
            assert inner is not None and inner is not outer
            h._signature("Inner has a separate owner")
        assert inner.closed and inner.values == {}
        assert h._STATEMENT_FEATURES.get() is outer
        assert h._signature("Outer retains a pure immutable signature") is original
    assert outer.closed and outer.values == {} and h._STATEMENT_FEATURES.get() is None


def test_concurrent_writers_have_separate_feature_contexts(tmp_path):
    roots = [str(tmp_path / name) for name in ("a", "b")]
    for root in roots:
        fact_store.migrate(root)
    barrier = threading.Barrier(2)

    def write(root):
        with fact_store.transaction(root, write=True):
            owner = h._STATEMENT_FEATURES.get()
            value = h._signature("Shared text still has separate transaction owners")
            barrier.wait(timeout=5)
            assert h._signature("Shared text still has separate transaction owners") is value
        assert owner.closed and owner.values == {} and h._STATEMENT_FEATURES.get() is None
        return owner, value

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, roots))
    assert results[0][0] is not results[1][0]
    assert results[0][1] is not results[1][1]
