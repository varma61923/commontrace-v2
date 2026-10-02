from __future__ import annotations

from commontrace import hierarchical


def test_t3_bitemporal_supersession_time_travel(isolated_store: str):
    """E2E-T3-CB-4: Complete bitemporal supersession pipeline allowing point-in-time time travel queries."""
    # 1. Historical fact valid in 2024
    f1, _ = hierarchical.add_fact(
        isolated_store,
        "Database encryption algorithm is AES-128-CBC.",
        category="security",
        valid_from="2024-01-01T00:00:00Z",
    )

    # 2. Supersede in 2025
    f1_old, f2 = hierarchical.supersede_fact(
        isolated_store,
        f1.id,
        "Database encryption algorithm is AES-256-GCM.",
    )

    assert f1_old.status == "superseded"
    assert f1_old.valid_until is not None
    assert f2.status == "active"
    assert f2.valid_from is not None

    # 3. Query as of mid-2024 (time-travel into the past)
    facts_2024 = hierarchical.list_facts(isolated_store, status="", as_of="2024-06-01T00:00:00Z")
    fact_ids_2024 = {f.id for f in facts_2024}
    assert f1.id in fact_ids_2024, "Past as_of query must return the original fact valid at that time"
    assert f2.id not in fact_ids_2024, "Past as_of query must not leak future facts"

    # 4. Query current active facts
    current_facts = hierarchical.list_facts(isolated_store, status="active")
    current_ids = {f.id for f in current_facts}
    assert f2.id in current_ids, "Current active query must return the latest superseded fact"
    assert f1.id not in current_ids, "Current active query must not return superseded facts"
