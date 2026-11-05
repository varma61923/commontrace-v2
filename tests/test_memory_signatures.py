"""Memory signatures: evidence transfer for memories that arrive with only an id and an embedding."""

from __future__ import annotations

import datetime
import math
import os
import random
import types

import pytest

from tests.test_gateway import call, recall
from unimem import allocation, causal_gate, effect_field, gateway, holdout_io, memory_signatures, nextwave_store

T0 = datetime.datetime(2026, 3, 1, 9, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture(autouse=True)
def _no_fsync(monkeypatch):
    monkeypatch.setattr(os, "fsync", lambda fd: None)


@pytest.fixture
def clock(monkeypatch):
    state = types.SimpleNamespace(now=T0)

    class Frozen(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return state.now

    frozen = types.SimpleNamespace(datetime=Frozen, timezone=datetime.timezone, timedelta=datetime.timedelta)
    monkeypatch.setattr(holdout_io, "datetime", frozen)
    monkeypatch.setattr(allocation, "datetime", frozen)
    return state


# -- signatures ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, model",
    [([], None), ([math.nan], None), ([True], None), ("0.1", None), ([0.0, 0.0], None), ([1.0], "bad label!")],
)
def test_embeddings_are_validated(value, model):
    with pytest.raises(ValueError):
        memory_signatures.embedding(value, model)


def test_signatures_are_recorded_once_per_revision_and_forgotten_from_disk(tmp_path):
    root = str(tmp_path)
    emb = memory_signatures.embedding([0.1, 0.2, 0.3], "siglip2")
    assert memory_signatures.observe(root, [("a", "re-verify the grasp pose", "r1", emb), ("b", None, None, None)]) == 1
    assert memory_signatures.observe(root, [("a", "re-verify the grasp pose", "r1", emb)]) == 0
    assert memory_signatures.observe(root, [("a", "re-verify the grasp pose twice", "r2", emb)]) == 1
    sig = memory_signatures.load(root)["a"]
    assert sig.key.startswith("r2|siglip2:") and sig.model == "siglip2" and sig.vector == (0.1, 0.2, 0.3)
    with open(memory_signatures.signatures_path(root), encoding="utf-8") as fh:
        assert "grasp" not in fh.read()
    assert memory_signatures.forget(root, ["a", "zzz"]) == ["a"]
    assert memory_signatures.load(root) == {} and memory_signatures.forget(root, ["a"]) == []


def test_recording_follows_field_mode_or_an_explicit_switch(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="s")
    assert not memory_signatures.enabled(root)
    memory_signatures.set_recording(root, True)
    assert memory_signatures.enabled(root)
    memory_signatures.set_recording(root, False)
    assert not memory_signatures.enabled(root)
    allocation.enable(root, allocation.Policy(mode=allocation.MODE_FIELD), now=T0 - datetime.timedelta(minutes=1))
    assert memory_signatures.enabled(root)


def test_the_canonical_erasure_cascade_removes_signatures(tmp_path):
    root = str(tmp_path)
    memory_signatures.observe(root, [("fact-1", "the dock door code", "r1", None)])
    removed = nextwave_store._erase_sources(root, ["fact-1"])
    assert {"kind": "memory-signatures", "id": "fact-1"} in removed
    assert memory_signatures.load(root) == {}


def test_kernels_average_cosines_over_shared_spaces_and_never_mix_models():
    k = effect_field.Kernel.combined(
        {"a": effect_field.term_bag("pick the red cube"), "b": effect_field.term_bag("pick the red cube")},
        {"a": ("clip", [1.0, 0.0]), "b": ("clip", [0.0, 1.0]), "c": ("clip", [1.0, 0.0]), "d": ("dino", [1.0, 0.0])},
    )
    assert k.kind == "lexical-tfidf-v2+clip+dino"
    assert k.similarity("a", "b") == pytest.approx(0.5)  # same words, orthogonal images
    assert k.similarity("a", "c") == pytest.approx(math.sqrt(0.5))  # c lives only in the image space
    assert k.similarity("c", "d") == 0.0
    assert k.similarity("a", "a") == pytest.approx(1.0)
    assert [m for m, _s in k.neighbors(["c"], ["a", "b", "d"])["c"]] == ["a"]


# -- a fleet through the gateway -------------------------------------------------------------------

CENTERS = {"regrasp": 0.20, "skip-check": -0.20, "log-battery": 0.0}


def _vector(rng: random.Random, family: str) -> list[float]:
    center = random.Random(family).random
    return [center() - 0.5 + rng.gauss(0, 0.08) for _ in range(16)]


def test_robots_with_only_embeddings_transfer_evidence_to_a_new_skill(tmp_path, clock):
    root = str(tmp_path / "fleet")
    holdout_io.configure(root, rate=0.5, salt="fleet")
    allocation.enable(root, allocation.Policy(era_occasions=200, mode=allocation.MODE_FIELD))
    clock.now += datetime.timedelta(seconds=10)
    g = gateway.Gateway(root, token="t" * 40)
    rng = random.Random(4)
    pool = [
        {"id": f"{family}-{i}", "embedding": _vector(rng, family), "embedding_model": "vla-skill-v1"}
        for family in CENTERS
        for i in range(8)
    ]
    effects = {item["id"]: CENTERS[item["id"].rsplit("-", 1)[0]] for item in pool}
    for i in range(600):
        offered = rng.sample(pool, 3)
        status, body = recall(g, f"t{i}", offered, agent_id=f"arm-{i % 5}")
        assert status == 200 and all("embedding" not in item for item in body["deliver"])
        shown = {item["id"] for item in body["deliver"]}
        progress = 0.5 + sum(effects[m] for m in shown) + rng.uniform(-0.1, 0.1)
        status, _ = call(g, "POST", "/v1/outcome", {"occasion_id": f"t{i}", "reward": min(1.0, max(0.0, progress))})
        assert status == 200
        clock.now += datetime.timedelta(seconds=30)

    newcomer = {"id": "skip-check-new", "embedding": _vector(rng, "skip-check"), "embedding_model": "vla-skill-v1"}
    assert recall(g, "t-pending", [newcomer])[0] == 200  # seen, but no outcome yet: unmeasured
    signatures = memory_signatures.load(root)
    assert set(effects) | {"skip-check-new"} <= set(signatures)
    assert all(not s.terms and s.model == "vla-skill-v1" for s in signatures.values())

    schedule = allocation.plan(root)
    assert schedule.basis["field"]["verdict"] == "VALIDATED", schedule.basis["field"]
    assert schedule.basis["field"]["kernel"] == "lexical-tfidf-v2+vla-skill-v1"
    assert schedule.rates["skip-check-new"] > 0.7
    model = causal_gate.fit(causal_gate.current_rows(root))
    assert model.lessons["regrasp-0"].mean > 0 > model.lessons["skip-check-0"].mean


def test_the_gateway_refuses_malformed_embeddings(tmp_path):
    root = str(tmp_path / "s")
    holdout_io.configure(root, rate=0.5, salt="s")
    g = gateway.Gateway(root, token="t" * 40)
    for item in (
        {"id": "a", "embedding": [1, "x"]},
        {"id": "a", "embedding": [0.1] * 4097},
        {"id": "a", "embedding": [0.1], "embedding_model": "no spaces"},
        {"id": "a", "embedding_model": "clip"},
    ):
        assert recall(g, "o1", [item])[0] == 400, item
