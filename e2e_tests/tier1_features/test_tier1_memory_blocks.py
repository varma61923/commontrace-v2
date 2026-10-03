from __future__ import annotations

import json
import os

from e2e_tests.harness.cli_runner import run_cli


def test_t1_block_create_and_get(isolated_store: str):
    res_set = run_cli(
        "block", "set", "persona",
        "Autonomous Staff Security Architect specialized in cloud systems.",
        "--max-chars", "1500",
        dest=isolated_store,
    )
    res_set.assert_success()
    assert "Saved block 'persona'" in res_set.stdout

    res_get = run_cli("block", "get", "persona", dest=isolated_store)
    res_get.assert_success()
    assert "Security Architect" in res_get.stdout

    block_file = os.path.join(isolated_store, "memory", "blocks", "persona.md")
    meta_file = os.path.join(isolated_store, "memory", "blocks", "persona.meta.json")
    assert os.path.exists(block_file), "Memory block markdown file must exist on disk"
    assert os.path.exists(meta_file), "Memory block metadata json must exist on disk"

    with open(meta_file, encoding="utf-8") as f:
        meta = json.load(f)
    assert meta["name"] == "persona"
    assert meta["max_chars"] == 1500
    assert len(meta["revision"]) == 16


def test_t1_block_append_text(isolated_store: str):
    run_cli(
        "block", "set", "guidelines",
        "1. Never commit plain credentials.\n",
        dest=isolated_store,
    ).assert_success()

    res_append = run_cli(
        "block", "append", "guidelines",
        "2. All database queries must use prepared statements.\n",
        dest=isolated_store,
    )
    res_append.assert_success()
    assert "Appended to block 'guidelines'" in res_append.stdout

    res_get = run_cli("block", "get", "guidelines", dest=isolated_store)
    res_get.assert_success()
    assert "Never commit plain credentials" in res_get.stdout
    assert "All database queries must use prepared statements" in res_get.stdout


def test_t1_block_atomic_substring_replace(isolated_store: str):
    run_cli(
        "block", "set", "project",
        "Current priority: optimize Cassandra write throughput.",
        dest=isolated_store,
    ).assert_success()

    res_replace = run_cli(
        "block", "replace", "project",
        "--old", "Cassandra write throughput",
        "--new", "PostgreSQL read replica latency",
        dest=isolated_store,
    )
    res_replace.assert_success()
    assert "Updated block 'project'" in res_replace.stdout

    res_get = run_cli("block", "get", "project", dest=isolated_store)
    res_get.assert_success()
    assert "PostgreSQL read replica latency" in res_get.stdout
    assert "Cassandra" not in res_get.stdout


def test_t1_block_list_and_delete(isolated_store: str):
    run_cli("block", "set", "persona", "Engineer A", dest=isolated_store).assert_success()
    run_cli("block", "set", "human", "Operator B", dest=isolated_store).assert_success()
    run_cli("block", "set", "project", "Project C", dest=isolated_store).assert_success()

    res_list = run_cli("block", "list", dest=isolated_store)
    res_list.assert_success()
    assert "persona" in res_list.stdout
    assert "human" in res_list.stdout
    assert "project" in res_list.stdout

    res_del = run_cli("block", "delete", "human", dest=isolated_store)
    res_del.assert_success()

    res_list_after = run_cli("block", "list", dest=isolated_store)
    res_list_after.assert_success()
    assert "human" not in res_list_after.stdout
    assert "persona" in res_list_after.stdout


def test_t1_block_sha256_revision_history_audit(isolated_store: str):
    run_cli("block", "set", "audit_test", "Version 1", dest=isolated_store).assert_success()
    run_cli("block", "append", "audit_test", " + Version 2", dest=isolated_store).assert_success()
    run_cli(
        "block", "replace", "audit_test", "--old", "Version 1", "--new", "Updated 1", dest=isolated_store
    ).assert_success()

    res_hist = run_cli("block", "history", "audit_test", dest=isolated_store)
    res_hist.assert_success()
    assert "audit_test" in res_hist.stdout

    history_file = os.path.join(isolated_store, "memory", "blocks", "history.jsonl")
    assert os.path.exists(history_file), "history.jsonl must exist"

    lines = [json.loads(line) for line in open(history_file, encoding="utf-8") if line.strip()]
    audit_entries = [entry for entry in lines if entry.get("block") == "audit_test"]
    assert len(audit_entries) >= 3, "Must have recorded all 3 mutation revisions"

    for entry in audit_entries:
        assert "revision" in entry
        assert "timestamp" in entry
        assert "action" in entry
        assert len(entry["revision"]) == 16


def test_t1_block_atomic_file_write_durability(isolated_store: str):
    content = "High-availability microservices architecture standard."
    run_cli("block", "set", "durability", content, dest=isolated_store).assert_success()

    blocks_dir = os.path.join(isolated_store, "memory", "blocks")
    files = os.listdir(blocks_dir)

    tmp_files = [f for f in files if f.endswith(".tmp")]
    assert len(tmp_files) == 0, f"Found lingering temporary files: {tmp_files}"

    with open(os.path.join(blocks_dir, "durability.md"), encoding="utf-8") as f:
        saved_content = f.read()
    assert saved_content.strip() == content
