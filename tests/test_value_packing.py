from __future__ import annotations

import random

import pytest

from unimem import compression, dosage


def test_seeded_knapsack_dominates_relevance_under_fixed_budget():
    rng = random.Random(41)
    for _ in range(100):
        candidates = [dosage.Candidate(str(i), "x" * rng.randrange(4, 80), relevance=1 - i / 20)
                      for i in range(20)]
        values = {item.slug: rng.random() for item in candidates}
        chosen, report = dosage.value_pack(candidates, values, max_tokens=60, max_items=8, duplicate_penalty=0)
        assert report["estimated_value"] + 1e-12 >= report["baseline_value"]
        assert sum((len(item.text) + 3) // 4 for item in chosen) <= 60
        assert len(chosen) <= 8
    hazard = dosage.Candidate("hazard", "safety" * 100, hazard=True)
    chosen, _ = dosage.value_pack([hazard], {"hazard": 0}, max_tokens=0, max_items=0)
    assert chosen == [hazard]


def test_cache_prefix_golden_stability_and_configured_bill():
    items = [{"id": "b", "text": "Memory B", "approved": True, "value": .2, "churn": 0},
             {"id": "a", "text": "Memory A", "approved": True, "value": .3, "churn": 0},
             {"id": "unapproved", "text": "Memory C", "approved": False, "value": 1, "churn": 0}]
    first = compression.cache_prefix(items)
    assert first == compression.cache_prefix(list(reversed(items)))
    assert first["text"].encode() == b"[a]\nMemory A\n\n[b]\nMemory B"
    assert first["context_bill"]["savings_usd"] is None
    priced = compression.cache_prefix(items, pricing={"uncached_per_million": 10, "cached_per_million": 2,
                                                     "uncached_ms_per_token": 1, "cached_ms_per_token": .2})
    assert priced["context_bill"]["savings_usd"] > 0


def test_packing_configuration_surface(tmp_path):
    pytest.importorskip("mcp")
    from tests.test_mcp_server import cli

    out = cli("retrieval", "--value-per-token", "--dest", str(tmp_path))
    assert out.returncode == 0, out.stderr
