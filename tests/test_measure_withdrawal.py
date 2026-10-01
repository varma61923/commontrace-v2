"""Automatic harm withdrawal for memory held anywhere (CausalMemory, the
adapters, file sources): a memory measured to make outcomes worse stops being
handed out, decided before arms are assigned, and only on readable evidence."""
from __future__ import annotations

import json
import os
import random

import pytest

from commontrace import evidence, holdout_io, retrieval_io
from commontrace import memory_adapters as ma
from commontrace import memory_sources as ms
from commontrace.measure import CausalMemory

ITEMS = [{"id": "harm", "memory": "always retry three times"}, {"id": "ok", "memory": "check the idempotency key"}]


def _store(root, policy="withdraw", salt="withdrawal-tests"):
    holdout_io.configure(str(root), rate=0.5, salt=salt)
    path = retrieval_io.config_path(str(root))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"harm_policy": policy}, fh)


def _seed_harm(root, n=300, harm_effect=-0.35, rates=None):
    """Occasions on which 'harm' makes outcomes much worse and 'ok' does nothing."""
    config = holdout_io.load_config(str(root))
    rng = random.Random(7)
    for i in range(n):
        rate = (rates or {}).get(i, config.rate)
        withheld = holdout_io.assign_and_log(
            str(root), ["harm", "ok"], occasion_id=f"seed-{i}", rate=rate, salt=config.salt)
        p = 0.65 + (harm_effect if "harm" not in withheld else 0.0)
        holdout_io.record_outcome(str(root), f"seed-{i}", rng.random() < p)


def _log_rows(root):
    path = holdout_io.holdout_log_path(str(root))
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_the_seeded_harmful_memory_is_withdrawn_and_the_others_are_not(tmp_path):
    _store(tmp_path)
    _seed_harm(tmp_path)
    memory = CausalMemory(lambda q, **kw: ITEMS, root=str(tmp_path))
    result = memory.recall_detailed("q", occasion_id="live-1")
    assert [i["id"] for i in result.items] == ["ok"]
    assert result.withdrawn["harm"]["verdict"] == "HURTS"
    assert result.withdrawn["harm"]["effect"] < 0


def test_the_default_policy_only_informs_and_changes_nothing(tmp_path):
    _store(tmp_path, policy="inform")
    _seed_harm(tmp_path)
    memory = CausalMemory(lambda q, **kw: ITEMS, root=str(tmp_path))
    delivered = {i["id"] for n in range(40) for i in memory.recall("q", occasion_id=f"live-{n}")}
    assert "harm" in delivered
    assert memory.recall_detailed("q", occasion_id="live-x").withdrawn == {}


def test_an_explicit_policy_overrides_the_stores(tmp_path):
    _store(tmp_path, policy="inform")
    _seed_harm(tmp_path)
    withdrawing = CausalMemory(lambda q, **kw: ITEMS, root=str(tmp_path), on_harm="withdraw")
    assert "harm" in withdrawing.recall_detailed("q", occasion_id="a").withdrawn
    _store(tmp_path, policy="withdraw")
    informing = CausalMemory(lambda q, **kw: ITEMS, root=str(tmp_path), on_harm="inform")
    assert informing.recall_detailed("q", occasion_id="b").withdrawn == {}


def test_withdrawal_happens_before_arms_are_assigned(tmp_path):
    """A withdrawn memory is never logged as treated or withheld on an occasion it
    was absent from -- that would bias every estimate involving it."""
    _store(tmp_path)
    _seed_harm(tmp_path)
    before = len(_log_rows(tmp_path))
    memory = CausalMemory(lambda q, **kw: ITEMS, root=str(tmp_path))
    for n in range(30):
        memory.recall("q", occasion_id=f"live-{n}")
    new = _log_rows(tmp_path)[before:]
    assert new and {row["lesson"] if "lesson" in row else row.get("lesson_slug") for row in new} == {"ok"}


def test_the_control_arm_is_identical_when_nothing_is_harmful(tmp_path):
    """Switching the policy on must not change what any occasion receives unless
    a memory has actually been measured to hurt."""
    delivered = {}
    for policy in ("inform", "withdraw"):
        root = tmp_path / policy
        _store(root, policy=policy, salt="same-salt")
        memory = CausalMemory(lambda q, **kw: ITEMS, root=str(root))
        delivered[policy] = [
            sorted(i["id"] for i in memory.recall("q", occasion_id=f"o{n}")) for n in range(200)
        ]
    assert delivered["inform"] == delivered["withdraw"]
    assert {tuple(d) for d in delivered["inform"]} >= {("harm", "ok"), ("ok",), ("harm",)}  # both arms occur


def test_a_compromised_experiment_withdraws_nothing(tmp_path):
    """Two holdout rates under one salt: the audit says COMPROMISED, there is no
    evidence, so nothing may be withdrawn on it."""
    _store(tmp_path)
    _seed_harm(tmp_path, rates={i: 0.1 for i in range(100, 300)})
    assert evidence.withdrawn(str(tmp_path), "withdraw") == {}
    memory = CausalMemory(lambda q, **kw: ITEMS, root=str(tmp_path))
    assert memory.recall_detailed("q", occasion_id="live").withdrawn == {}


def test_a_new_randomization_gives_a_withdrawn_memory_a_second_trial(tmp_path):
    _store(tmp_path)
    _seed_harm(tmp_path)
    memory = CausalMemory(lambda q, **kw: ITEMS, root=str(tmp_path), check_every=1)
    assert "harm" in memory.recall_detailed("q", occasion_id="a").withdrawn
    holdout_io.configure(str(tmp_path), rate=0.3)  # rotates the salt
    assert memory.recall_detailed("q", occasion_id="b").withdrawn == {}


def test_a_pinned_memory_is_never_withdrawn(tmp_path):
    _store(tmp_path)
    _seed_harm(tmp_path)
    memory = CausalMemory(lambda q, **kw: ITEMS, root=str(tmp_path), pinned=["harm"])
    # Pinned is delivered on every occasion; "ok" is subject to the random holdout.
    assert all("harm" in {i["id"] for i in memory.recall("q", occasion_id=f"live-{n}")} for n in range(20))


def test_the_evidence_is_re_read_only_every_check_every_recalls(tmp_path, monkeypatch):
    _store(tmp_path)
    calls = []
    monkeypatch.setattr(evidence, "withdrawn", lambda root, policy: calls.append(policy) or {})
    memory = CausalMemory(lambda q, **kw: ITEMS, root=str(tmp_path), check_every=25)
    for n in range(100):
        memory.recall("q", occasion_id=f"o{n}")
    assert len(calls) == 4 and set(calls) == {"withdraw"}


def test_the_default_does_not_read_the_evidence_on_every_recall(tmp_path, monkeypatch):
    _store(tmp_path)
    calls = []
    monkeypatch.setattr(evidence, "withdrawn", lambda root, policy: calls.append(1) or {})
    memory = CausalMemory(lambda q, **kw: ITEMS, root=str(tmp_path))
    for n in range(60):
        memory.recall("q", occasion_id=f"o{n}")
    assert len(calls) < 5


@pytest.mark.parametrize("kwargs", [{"on_harm": "delete"}, {"check_every": 0}, {"check_every": 1.5}])
def test_bad_options_are_refused(tmp_path, kwargs):
    with pytest.raises(ValueError):
        CausalMemory(lambda q, **kw: ITEMS, root=str(tmp_path), **kwargs)


def test_a_failure_reading_the_evidence_leaves_recall_as_it_was(tmp_path, monkeypatch):
    _store(tmp_path)
    monkeypatch.setattr(evidence, "for_lessons", lambda root: (_ for _ in ()).throw(RuntimeError("boom")))
    memory = CausalMemory(lambda q, **kw: ITEMS, root=str(tmp_path))
    assert memory.recall_detailed("q", occasion_id="a").withdrawn == {}


# --- Adapters --------------------------------------------------------------------


class _Mem0:
    def __init__(self):
        self.deleted = []

    def search(self, query, **kw):
        return {"results": [{"id": i["id"], "memory": i["memory"]} for i in ITEMS]}

    def delete(self, memory_id):
        self.deleted.append(memory_id)


def test_an_adapter_withdraws_automatically_and_never_deletes_by_default(tmp_path):
    _store(tmp_path)
    _seed_harm(tmp_path)
    client = _Mem0()
    memory = ma.MeasuredMemory(ma.Mem0Adapter(client), root=str(tmp_path))
    result = memory.recall_detailed("q", occasion_id="live")
    assert "harm" not in {i.id for i in result.items} and "harm" in result.withdrawn
    assert client.deleted == []
    assert not ms.blocked_ids_for_key(str(tmp_path), memory.source_key)  # follows the evidence, not a file


def test_deleting_at_the_source_is_opt_in_and_blocklists_first(tmp_path):
    _store(tmp_path)
    _seed_harm(tmp_path)
    client = _Mem0()
    memory = ma.MeasuredMemory(ma.Mem0Adapter(client), root=str(tmp_path), delete_harmful=True)
    memory.recall_detailed("q", occasion_id="live")
    assert client.deleted == ["harm"]
    assert ms.blocked_ids_for_key(str(tmp_path), memory.source_key) == {"harm"}


def test_a_failed_source_delete_still_stops_delivery(tmp_path):
    _store(tmp_path)
    _seed_harm(tmp_path)

    class Failing(_Mem0):
        def delete(self, memory_id):
            raise RuntimeError("source down")

    memory = ma.MeasuredMemory(ma.Mem0Adapter(Failing()), root=str(tmp_path), delete_harmful=True)
    assert "harm" not in {i.id for i in memory.recall("q", occasion_id="live")}
    assert ms.blocked_ids_for_key(str(tmp_path), memory.source_key) == {"harm"}


def test_a_source_that_cannot_delete_is_only_blocked_from_delivery(tmp_path):
    _store(tmp_path)
    _seed_harm(tmp_path)
    adapter = ma.Mem0Adapter(_Mem0())
    adapter.can_delete = False
    memory = ma.MeasuredMemory(adapter, root=str(tmp_path), delete_harmful=True)
    assert "harm" not in {i.id for i in memory.recall("q", occasion_id="live")}
    assert not ms.blocked_ids_for_key(str(tmp_path), memory.source_key)  # nothing to delete, nothing written


# --- File sources ------------------------------------------------------------------


def test_a_file_source_reports_a_harmfully_withdrawn_section_apart_from_the_random_holdout(
    tmp_path, monkeypatch
):
    _store(tmp_path)
    source = tmp_path / "AGENTS.md"
    source.write_text("intro\n## Retries\nretry 3 times\n## Keys\nuse idempotency keys\n", encoding="utf-8")
    sections = ms.FileMemorySource(str(source), root=str(tmp_path)).sections()
    bad = sections[0].id
    monkeypatch.setattr(evidence, "withdrawn", lambda root, policy: {bad: {"verdict": "HURTS"}})
    rendered = ms.FileMemorySource(str(source), root=str(tmp_path)).render("occasion-1")
    assert rendered.harmful == [bad] and bad not in rendered.withheld
    assert "retry 3 times" not in rendered.text
    assert rendered.blocked == []
    assert "idempotency" in rendered.text or "keys" in rendered.withheld  # the other section follows the holdout


