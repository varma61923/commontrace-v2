"""Exact community adjacency preserves every signal without redundant cliques."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from commontrace import communities, entity_store, hierarchical


def test_adjacency_matches_pairwise_reference_for_overlapping_duplicate_groups(tmp_path, monkeypatch):
    rng = random.Random(2026)
    nodes = {f"n{i}": {"source_traces": [f"t{j}" for j in range(12) if rng.random() < 0.3],
                        "tags": [f"g{j}" for j in range(12) if rng.random() < 0.3]}
             for i in range(40)}
    for node in nodes.values():
        node["tags"] += ["global1", "global2"]
    entities = {f"e{i}": {"memory_ids": rng.sample(list(nodes), 10) + ["nonexistent"]}
                for i in range(8)}
    monkeypatch.setattr(entity_store, "load_entities", lambda _root: entities)
    reference = {identity: set() for identity in nodes}
    for first, one in nodes.items():
        for second, two in nodes.items():
            if first == second:
                continue
            if (set(one["tags"]) & set(two["tags"])
                    or set(one["source_traces"]) & set(two["source_traces"])
                    or any(first in entity["memory_ids"] and second in entity["memory_ids"]
                           for entity in entities.values())):
                reference[first].add(second)
    expected = {identity: sorted(neighbors) for identity, neighbors in reference.items()}
    actual = communities._build_adjacency(str(tmp_path), nodes)
    assert actual == expected
    assert communities._label_propagation(list(nodes), actual) == communities._label_propagation(list(nodes), expected)
    assert all(len(neighbors) == len(nodes) - 1 for neighbors in actual.values())


def test_community_input_excludes_expired_future_and_forgotten_facts(tmp_path):
    root = str(tmp_path)
    now = datetime.now(timezone.utc)
    past, future = (now - timedelta(days=1)).isoformat(), (now + timedelta(days=1)).isoformat()
    facts = {}
    for identity, kwargs in (("current", {}), ("expired", {"expires_at": past}),
                             ("future", {"valid_from": future}), ("forgotten", {"forgotten": True}),
                             ("ended", {"valid_from": (now - timedelta(days=2)).isoformat(), "valid_until": past})):
        args = dict(id=identity, statement=identity, category="constraint", scopes=[], confidence=0.8,
                    confirmations=2, valid_from=past, valid_until=None)
        args.update(kwargs)
        facts[identity] = hierarchical.AtomicFact(**args)
    hierarchical.save_facts(root, facts)
    assert list(communities._load_nodes(root)) == ["current"]
