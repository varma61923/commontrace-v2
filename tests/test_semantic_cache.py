from __future__ import annotations

import concurrent.futures
import threading
import time

import pytest

from commontrace import semantic

np = pytest.importorskip("numpy")


class Encoder:
    def __init__(self):
        self.calls = 0
        self.lock = threading.Lock()

    def encode(self, texts, **kwargs):
        with self.lock:
            self.calls += 1
        time.sleep(0.01)
        return np.asarray([[len(text), 1] for text in texts], dtype=np.float32)


@pytest.fixture
def encoder(monkeypatch):
    semantic._vectors.clear()
    model = Encoder()
    monkeypatch.setattr(semantic, "load_model", lambda: model)
    yield model
    semantic._vectors.clear()


def test_concurrent_identical_batches_coalesce_and_arrays_are_isolated(encoder):
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        arrays = list(pool.map(lambda _: semantic.encode(["deployment", "retry"]), range(16)))
    assert encoder.calls == 1
    arrays[0][0, 0] = 999
    assert arrays[1][0, 0] == len("deployment")
    assert semantic.encode(["deployment", "retry"])[0, 0] == len("deployment")


def test_text_boundaries_order_and_model_identity_are_cache_keys(encoder, monkeypatch):
    semantic.encode(["ab", "c"])
    semantic.encode(["a", "bc"])
    semantic.encode(["c", "ab"])
    assert encoder.calls == 3
    replacement = Encoder()
    monkeypatch.setattr(semantic, "load_model", lambda: replacement)
    semantic.encode(["ab", "c"])
    assert replacement.calls == 1


def test_bad_vectors_fail_without_poisoning_cache(encoder, monkeypatch):
    original = encoder.encode
    monkeypatch.setattr(encoder, "encode", lambda *_a, **_kw: np.array([[np.nan, 1]]))
    with pytest.raises(semantic.SemanticUnavailable, match="invalid vectors"):
        semantic.encode(["retry"])
    monkeypatch.setattr(encoder, "encode", original)
    assert np.isfinite(semantic.encode(["retry"])).all()
    assert encoder.calls == 1


def test_empty_batch_preserves_error_contract(encoder):
    with pytest.raises(semantic.SemanticUnavailable, match="nothing to encode"):
        semantic.encode([])


def test_non_weakref_encoder_preserves_structural_interface(monkeypatch):
    class SlottedEncoder:
        __slots__ = ()

        def encode(self, texts, **kwargs):
            return np.ones((len(texts), 2), dtype=np.float32)

    monkeypatch.setattr(semantic, "load_model", SlottedEncoder)
    assert semantic.encode(["retry"]).shape == (1, 2)
