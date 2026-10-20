from random import Random
from types import SimpleNamespace

import pytest

from hub.memory_records import _ATOMIC_FACT_FORMAT, _erasure_closure


def _reference_closure(rows, identity):
    """Previous fixed-point semantics, retained as an independent test oracle."""
    deleted, source_episodes = {identity}, set()
    while True:
        citations = [(source, row.record["scope"]) for row in rows if row.id in deleted
                     for source in row.record["payload"].get("source_message_ids", [])]
        sources = {row.id for row in rows if row.record["kind"] == "episode"
                   and (row.id, row.record["scope"]) in citations}
        source_episodes.update(sources)
        derived = sources | {row.id for row in rows
                             if any(set(ids) & deleted for ids in row.record["relations"].values())}
        derived |= {row.id for row in rows
                    if row.record["provenance"].get("atomic_fact_format") == _ATOMIC_FACT_FORMAT
                    and any(isinstance(receipt, dict) and receipt.get("source_id") in deleted
                            for receipt in row.record["payload"].get("evidence", []))}
        if derived <= deleted:
            return deleted, source_episodes
        deleted |= derived


@pytest.mark.parametrize("seed", range(32))
def test_erasure_closure_preserves_historical_scope_and_evidence_semantics(seed):
    random = Random(seed)
    rows = []
    for _ in range(80):
        identity = str(random.randrange(32))
        # Repeated identities model historical revisions, including citations
        # removed from a later payload. Random links include cycles and orphans.
        rows.append(SimpleNamespace(id=identity, record={
            "scope": {"tenant": "owner", "user": random.choice(["a", "b"])},
            "kind": random.choice(["episode", "fact", "observation"]),
            "relations": {"derives": [str(random.randrange(34)) for _ in range(random.randrange(3))]},
            "payload": {"source_message_ids": [str(random.randrange(34))],
                        "evidence": [{"source_id": str(random.randrange(34))}, "untyped"]},
            "provenance": {"atomic_fact_format": random.choice([_ATOMIC_FACT_FORMAT, "untyped"])},
        }))
    identity = str(random.randrange(34))
    assert _erasure_closure(rows, identity) == _reference_closure(rows, identity)
    random.shuffle(rows)
    assert _erasure_closure(rows, identity) == _reference_closure(rows, identity)


def test_long_erasure_chain_indexes_history_once():
    class Rows(list):
        scans = 0

        def __iter__(self):
            self.scans += 1
            return super().__iter__()

    rows = Rows(SimpleNamespace(id=str(index), record={
        "kind": "observation", "scope": {}, "provenance": {}, "payload": {},
        "relations": {"derives": [str(index - 1)]} if index else {},
    }) for index in range(5000))
    deleted, sources = _erasure_closure(rows, "0")
    assert deleted == {str(index) for index in range(5000)}
    assert not sources
    assert rows.scans == 1
