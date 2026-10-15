from commontrace import fact_store, hierarchical
from commontrace.privacy_erasure import purge_fact


def test_hard_fact_erasure_cascades_history_and_vectors(tmp_path):
    root = str(tmp_path)
    fact_store.migrate(root)
    fact, _ = hierarchical.add_fact(root, "privatephrase parser checksum")
    result = purge_fact(root, fact.id, reason="owner request")
    assert fact.id in result["deleted_facts"]
    assert not hierarchical.load_facts(root)
    assert not fact_store.events(root)
    assert b"privatephrase" not in (tmp_path / "memory" / "facts" / "facts.sqlite3").read_bytes()


def test_hard_erasure_removes_dense_candidates_and_control_revisions(tmp_path):
    import asyncio
    import sqlite3

    from commontrace import memory_control
    from commontrace.fact_vector_projection import index_path
    from commontrace.vector_store import SQLiteVectorIndex, VectorRecord

    root = str(tmp_path)
    fact_store.migrate(root)
    fact, _ = hierarchical.add_fact(root, "sensitivephrase parser checksum")
    memory_control.put(
        root, "mental-model", "sensitivephrase derivative", record_id="derived", data={"sources": [fact.id]}
    )

    async def seed():
        index = await SQLiteVectorIndex.open(
            index_path(root), tenant="local", namespace="facts", model="test", dimension=2
        )
        await index.upsert([VectorRecord(fact.id, [1, 0])])
        await index.close()

    asyncio.run(seed())
    purge_fact(root, fact.id, reason="erase all materializations")
    with sqlite3.connect(index_path(root)) as db:
        assert db.execute("SELECT count(*) FROM commontrace_vectors").fetchone()[0] == 0
    assert not list((tmp_path / "memory" / "controls" / "mental-model").glob("*.md"))
