from __future__ import annotations

import os

import pytest

from commontrace import interop
from commontrace.cli import main


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _seed_store(root) -> dict[str, str]:
    from commontrace import frontmatter, graph, hierarchical, memory_blocks

    root_s = str(root)
    main(["init", "--agent-type", "code", "--dest", root_s])
    fm = {
        "name": "lesson_cogx_demo",
        "description": "demo lesson for cogx round-trip",
        "tags": ["cogx"],
        "agent_type": "code",
        "domain": "testing",
        "importance": 3,
        "importance_rationale": "round-trip fixture",
        "applies_when": "testing cogx export",
        "do_not_apply_when": "never in this fixture",
        "uses": 0,
        "last_hit": "NEVER",
        "status": "active",
    }
    ldir = os.path.join(root_s, "memory", "lessons")
    os.makedirs(ldir, exist_ok=True)
    frontmatter.write(os.path.join(ldir, "lesson_cogx_demo.md"), fm, "Always round-trip.")
    fact, _ = hierarchical.add_fact(root_s, "CogX keeps memory portable.", scopes=["t1"])
    graph.add_node(root_s, "svc-auth", entity_type="service", name="auth")
    graph.add_edge(root_s, "svc-auth", "lesson:cogx_demo", "relates_to")
    memory_blocks.set_block(root_s, "persona", "I am a careful engineer.")
    return {"fact_id": fact.id}


class TestEnvelope:
    def test_write_read_round_trip(self, tmp_path):
        records = [
            interop.make_record("lesson", {"slug": "a", "frontmatter": {"name": "lesson_a"}, "body": "b"}),
            interop.make_record("fact", {"statement": "s"}),
            interop.make_record("block", {"name": "persona", "content": "hi"}),
        ]
        path = str(tmp_path / "mem.cogx.json")
        interop.write_cogx(path, records)
        envelope = interop.read_cogx(path)
        assert envelope["format"] == "cogx/v1"
        assert envelope["schema_version"] == 1
        assert envelope["exported_at"]
        assert envelope["records"] == records

    def test_build_envelope_accepts_envelope_dict(self):
        records = [interop.make_record("fact", {"statement": "s"})]
        envelope = interop.build_envelope(records)
        assert interop.build_envelope(envelope)["records"] == records

    def test_loads_rejects_non_cogx(self):
        with pytest.raises(ValueError, match="format"):
            interop.loads_cogx('{"format": "nope", "records": []}')
        with pytest.raises(ValueError, match="records"):
            interop.loads_cogx('{"format": "cogx/v1", "schema_version": 1, "records": {}}')
        with pytest.raises(ValueError, match="kind"):
            interop.loads_cogx('{"format": "cogx/v1", "schema_version": 1, '
                               '"records": [{"kind": "nope", "data": {}}]}')
        with pytest.raises(ValueError, match="invalid JSON"):
            interop.loads_cogx("{not json")

    def test_make_record_rejects_bad_kind(self):
        with pytest.raises(ValueError, match="unknown record kind"):
            interop.make_record("snapshot", {})

    def test_detect_file_format(self, tmp_path):
        cogx_path = str(tmp_path / "a.json")
        interop.write_cogx(cogx_path, [interop.make_record("fact", {"statement": "s"})])
        assert interop.detect_file_format(cogx_path) == "cogx"
        jsonl_path = str(tmp_path / "b.jsonl")
        with open(jsonl_path, "w", encoding="utf-8") as fh:
            fh.write('{"title": "t", "context": "c", "solution": "s"}\n')
        assert interop.detect_file_format(jsonl_path) == "jsonl"
        csv_path = str(tmp_path / "c.csv")
        with open(csv_path, "w", encoding="utf-8") as fh:
            fh.write("title,context,solution\nt,c,s\n")
        assert interop.detect_file_format(csv_path) == "csv"


class TestCliRoundTrip:
    def test_export_import_round_trip(self, store, tmp_path, capsys):
        from commontrace import graph, hierarchical, memory_blocks

        seeded = _seed_store(store)
        capsys.readouterr()
        out = str(tmp_path / "handoff.json")
        rc = main(["export", "--format", "cogx", "--out", out, "--dest", str(store)])
        assert rc == 0
        capsys.readouterr()

        envelope = interop.read_cogx(out)
        kinds = {r["kind"] for r in envelope["records"]}
        assert {"lesson", "fact", "graph_node", "graph_edge", "block"} <= kinds

        dest = tmp_path / "store2"
        main(["init", "--agent-type", "code", "--dest", str(dest)])
        capsys.readouterr()
        rc = main(["import", out, "--agent-type", "code", "--dest", str(dest)])
        assert rc == 0

        lesson_path = os.path.join(str(dest), "memory", "lessons", "lesson_cogx_demo.md")
        assert os.path.isfile(lesson_path)
        with open(lesson_path, encoding="utf-8") as fh:
            assert "Always round-trip." in fh.read()
        facts = hierarchical.load_facts(str(dest))
        assert seeded["fact_id"] in facts
        assert facts[seeded["fact_id"]].statement == "CogX keeps memory portable."
        assert memory_blocks.get_block(str(dest), "persona").content == "I am a careful engineer."
        assert "svc-auth" in graph.load_nodes(str(dest))

    def test_export_kind_traces_has_no_cogx_counterpart(self, store, tmp_path, capsys):
        _seed_store(store)
        capsys.readouterr()
        rc = main(["export", "--format", "cogx", "--kind", "traces",
                   "--out", str(tmp_path / "x.json"), "--dest", str(store)])
        assert rc == 1
        assert "no cogx counterpart" in capsys.readouterr().err

    def test_export_kind_lessons_exports_only_lessons(self, store, tmp_path, capsys):
        _seed_store(store)
        capsys.readouterr()
        out = str(tmp_path / "lessons-only.json")
        rc = main(["export", "--format", "cogx", "--kind", "lessons",
                   "--out", out, "--dest", str(store)])
        assert rc == 0
        kinds = {r["kind"] for r in interop.read_cogx(out)["records"]}
        assert kinds == {"lesson"}


class TestMem0Adapter:
    MEM0 = {
        "memories": [
            {"id": "m1", "memory": "User prefers dark mode.", "user_id": "u1"},
            {"id": "m2", "memory": "Repo uses ruff with line-length 120.",
             "metadata": {"category": "tool_rule"}},
            {"id": "m3", "memory": "   "},
        ]
    }

    def test_memories_become_fact_records(self):
        records = interop.import_mem0_dump(self.MEM0)
        assert len(records) == 2
        assert all(r["kind"] == "fact" for r in records)
        assert records[0]["data"]["statement"] == "User prefers dark mode."
        assert records[0]["data"]["scopes"] == ["u1"]
        assert records[0]["data"]["source_id"] == "m1"
        assert records[1]["data"]["category"] == "tool_rule"

    def test_bare_list_shape(self):
        records = interop.import_mem0_dump([{"memory": "Likes vim.", "user_id": "u9"}])
        assert len(records) == 1
        assert records[0]["data"]["scopes"] == ["u9"]

    def test_empty_dump_yields_no_records(self):
        assert interop.import_mem0_dump({}) == []
        assert interop.import_mem0_dump([]) == []
        assert interop.import_mem0_dump(None) == []

    def test_mem0_records_apply_to_store(self, store):
        from commontrace import hierarchical

        main(["init", "--agent-type", "code", "--dest", str(store)])
        counts = interop.apply_store(str(store), interop.import_mem0_dump(self.MEM0))
        assert counts["fact"] == 2
        statements = [f.statement for f in hierarchical.load_facts(str(store)).values()]
        assert "User prefers dark mode." in statements


class TestZepAdapter:
    ZEP = {
        "episodes": [
            {"uuid": "e1", "title": "Onboarding call",
             "content": "User signed up and enabled SSO.", "created_at": "2026-01-02T00:00:00+00:00"},
            {"uuid": "e2", "name": "Blank episode", "content": ""},
        ]
    }

    def test_episodes_become_lesson_records(self):
        records = interop.import_zep_episodes(self.ZEP)
        assert len(records) == 1
        rec = records[0]
        assert rec["kind"] == "lesson"
        assert rec["data"]["slug"] == "e1"
        assert "SSO" in rec["data"]["body"]
        assert rec["data"]["frontmatter"]["agent_type"] == "general"
        assert rec["data"]["frontmatter"]["status"] == "review"

    def test_zep_records_apply_to_store(self, store):
        main(["init", "--agent-type", "code", "--dest", str(store)])
        counts = interop.apply_store(str(store), interop.import_zep_episodes(self.ZEP))
        assert counts["lesson"] == 1
        lesson_path = os.path.join(str(store), "memory", "lessons", "lesson_e1.md")
        assert os.path.isfile(lesson_path)

    def test_empty_dump_yields_no_records(self):
        assert interop.import_zep_episodes({}) == []
        assert interop.import_zep_episodes([]) == []


class TestLettaAdapter:
    LETTA = {"blocks": [
        {"label": "persona", "value": "I am a careful engineer.", "limit": 2000},
        {"label": "human", "value": "Val likes concise replies."},
        {"label": "empty", "value": "  "},
    ]}

    def test_blocks_become_block_records(self):
        records = interop.import_letta_blocks(self.LETTA)
        assert len(records) == 2
        assert all(r["kind"] == "block" for r in records)
        by_name = {r["data"]["name"]: r["data"] for r in records}
        assert by_name["persona"]["content"] == "I am a careful engineer."
        assert by_name["human"]["max_chars"] >= len("Val likes concise replies.")

    def test_mapping_shape(self):
        records = interop.import_letta_blocks({"persona": "Helpful.", "human": "Night owl."})
        assert {r["data"]["name"] for r in records} == {"persona", "human"}

    def test_letta_records_apply_to_store(self, store):
        from commontrace import memory_blocks

        main(["init", "--agent-type", "code", "--dest", str(store)])
        counts = interop.apply_store(str(store), interop.import_letta_blocks(self.LETTA))
        assert counts["block"] == 2
        assert memory_blocks.get_block(str(store), "persona").content == "I am a careful engineer."

    def test_empty_dump_yields_no_records(self):
        assert interop.import_letta_blocks({}) == []
        assert interop.import_letta_blocks([]) == []


def test_apply_store_skips_bad_records(store):
    main(["init", "--agent-type", "code", "--dest", str(store)])
    counts = interop.apply_store(str(store), [
        {"kind": "nope", "data": {}},
        {"kind": "fact", "data": {"statement": ""}},
        "not-a-record",
        interop.make_record("fact", {"statement": "kept"}),
    ])
    assert counts == {"lesson": 0, "fact": 1, "graph_node": 0, "graph_edge": 0,
                      "block": 0, "trace": 0, "skipped": 3}
