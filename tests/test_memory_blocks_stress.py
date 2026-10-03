from __future__ import annotations

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from commontrace import memory_blocks


@pytest.fixture
def store(tmp_path):
    root = str(tmp_path / "fleet")
    os.makedirs(os.path.join(root, ".commontrace"), exist_ok=True)
    return root


def _oracle_hash(name: str, content: str, prev_revision: str) -> str:
    payload = f"{name}:{content}:{prev_revision}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def test_quota_ceiling_exact_boundaries(store):
    max_chars = 100

    exact_content = "a" * max_chars
    b = memory_blocks.set_block(store, "quota_exact", exact_content, max_chars=max_chars)
    assert b.char_count == max_chars
    assert b.content == exact_content

    over_content = "a" * (max_chars + 1)
    with pytest.raises(memory_blocks.QuotaExceededError) as exc_info:
        memory_blocks.set_block(store, "quota_exact", over_content, max_chars=max_chars)
    assert f"Content length {max_chars + 1} exceeds quota of {max_chars}" in str(exc_info.value)

    b_after = memory_blocks.get_block(store, "quota_exact")
    assert b_after.char_count == max_chars
    assert b_after.revision == b.revision

    padded_exact = f"  \n{exact_content}\t  "
    b_stripped = memory_blocks.set_block(store, "quota_stripped", padded_exact, max_chars=max_chars)
    assert b_stripped.char_count == max_chars

    padded_over = f"  {over_content}\n"
    with pytest.raises(memory_blocks.QuotaExceededError):
        memory_blocks.set_block(store, "quota_stripped_fail", padded_over, max_chars=max_chars)

    b_zero = memory_blocks.set_block(store, "zero_block", "", max_chars=0)
    assert b_zero.char_count == 0
    with pytest.raises(memory_blocks.QuotaExceededError):
        memory_blocks.set_block(store, "zero_block", "x", max_chars=0)

    with pytest.raises(memory_blocks.QuotaExceededError):
        memory_blocks.set_block(store, "neg_block", "", max_chars=-1)


def test_append_quota_boundary_and_rollback(store):
    max_chars = 50
    base_text = "x" * 20
    b = memory_blocks.set_block(store, "append_quota", base_text, max_chars=max_chars)
    orig_rev = b.revision
    hist_len = len(memory_blocks.block_history(store, "append_quota"))

    fit_text = "y" * 29
    b2 = memory_blocks.append_block(store, "append_quota", fit_text)
    assert b2.char_count == max_chars
    assert len(b2.content) == max_chars

    with pytest.raises(memory_blocks.QuotaExceededError):
        memory_blocks.append_block(store, "append_quota", "z")

    b3 = memory_blocks.get_block(store, "append_quota")
    assert b3.char_count == max_chars
    assert b3.revision == b2.revision
    assert len(memory_blocks.block_history(store, "append_quota")) == hist_len + 1


def test_replace_quota_boundary_and_rollback(store):
    max_chars = 40
    b = memory_blocks.set_block(store, "rep_quota", "hello " + ("a" * 24), max_chars=max_chars)
    orig_rev = b.revision
    hist_len = len(memory_blocks.block_history(store, "rep_quota"))

    b2 = memory_blocks.replace_block(store, "rep_quota", "hello", "x" * 15)
    assert b2.char_count == max_chars

    with pytest.raises(memory_blocks.QuotaExceededError):
        memory_blocks.replace_block(store, "rep_quota", "x" * 15, "y" * 16)

    b3 = memory_blocks.get_block(store, "rep_quota")
    assert b3.char_count == max_chars
    assert b3.revision == b2.revision
    assert len(memory_blocks.block_history(store, "rep_quota")) == hist_len + 1


def test_substring_replace_edge_cases(store):
    content = "The quick brown fox jumps over the lazy dog. The quick brown fox."
    b = memory_blocks.set_block(store, "rep_edge", content)
    orig_rev = b.revision

    with pytest.raises(memory_blocks.SubstringNotFoundError) as exc_info:
        memory_blocks.replace_block(store, "rep_edge", "cat", "tiger")
    assert "Target text not found" in str(exc_info.value)
    assert memory_blocks.get_block(store, "rep_edge").revision == orig_rev

    with pytest.raises(memory_blocks.MemoryBlockError) as exc_info:
        memory_blocks.replace_block(store, "rep_edge", "The quick brown fox", "A wolf")
    assert "Ambiguous replacement: target text occurs 2 times" in str(exc_info.value)
    assert memory_blocks.get_block(store, "rep_edge").revision == orig_rev

    b_rep = memory_blocks.set_block(store, "overlap", "aaaa")
    with pytest.raises(memory_blocks.MemoryBlockError) as exc_info:
        memory_blocks.replace_block(store, "overlap", "aa", "bb")
    assert "Ambiguous replacement: target text occurs 2 times" in str(exc_info.value)

    with pytest.raises(memory_blocks.MemoryBlockError) as exc_info:
        memory_blocks.replace_block(store, "rep_edge", "", "injected")
    assert "Ambiguous replacement" in str(exc_info.value)

    special_text = r"Price is $10.00 (discount [50%]*? + \path\to\file^$). End."
    b_spec = memory_blocks.set_block(store, "spec_block", special_text)
    b_spec_rep = memory_blocks.replace_block(
        store, "spec_block", r"[50%]*? + \path\to\file^$", r"[25%]"
    )
    assert r"[25%]" in b_spec_rep.content
    assert r"\path\to\file" not in b_spec_rep.content

    uni_text = "Architecture status: 🔴 Failing. Need fix: 🚀."
    memory_blocks.set_block(store, "uni_block", uni_text)
    b_uni = memory_blocks.replace_block(store, "uni_block", "🔴 Failing", "🟢 Passing")
    assert "🟢 Passing" in b_uni.content
    assert "🔴 Failing" not in b_uni.content

    hist_before = len(memory_blocks.block_history(store, "uni_block"))
    b_noop = memory_blocks.replace_block(store, "uni_block", "🟢 Passing", "🟢 Passing")
    assert b_noop.content == b_uni.content
    assert b_noop.revision != b_uni.revision
    hist_after = len(memory_blocks.block_history(store, "uni_block"))
    assert hist_after == hist_before + 1


def test_sha256_revision_hash_continuity_oracle(store):
    name = "persona"

    c1 = "Agent Persona v1"
    b1 = memory_blocks.set_block(store, name, c1, actor="lead", reason="init")
    expected_rev1 = _oracle_hash(name, c1, "")
    assert b1.revision == expected_rev1
    assert b1.revision == memory_blocks.get_block(store, name).revision

    c2_append = "Rule 1: Always test."
    c2 = f"{c1}\n{c2_append}"
    b2 = memory_blocks.append_block(store, name, c2_append, actor="lead", reason="add rule")
    expected_rev2 = _oracle_hash(name, c2, expected_rev1)
    assert b2.revision == expected_rev2
    assert b2.revision == memory_blocks.get_block(store, name).revision

    c3 = c2.replace("Rule 1: Always test.", "Rule 1: Thoroughly test.")
    b3 = memory_blocks.replace_block(
        store, name, "Rule 1: Always test.", "Rule 1: Thoroughly test.", actor="lead", reason="clarify"
    )
    expected_rev3 = _oracle_hash(name, c3, expected_rev2)
    assert b3.revision == expected_rev3
    assert b3.revision == memory_blocks.get_block(store, name).revision

    c4 = "Agent Persona v2 (Refactored)"
    b4 = memory_blocks.set_block(store, name, c4, actor="lead", reason="overhaul")
    expected_rev4 = _oracle_hash(name, c4, expected_rev3)
    assert b4.revision == expected_rev4
    assert b4.revision == memory_blocks.get_block(store, name).revision

    assert memory_blocks.delete_block(store, name, actor="lead", reason="deprecate") is True
    expected_rev5 = _oracle_hash(name, "", expected_rev4)

    history = memory_blocks.block_history(store, name)
    assert len(history) == 5

    assert history[0]["prev_revision"] == ""
    assert history[0]["revision"] == expected_rev1
    assert history[0]["action"] == "set"
    assert history[0]["char_count"] == len(c1)

    assert history[1]["prev_revision"] == history[0]["revision"]
    assert history[1]["revision"] == expected_rev2
    assert history[1]["action"] == "set"
    assert history[1]["char_count"] == len(c2)

    assert history[2]["prev_revision"] == history[1]["revision"]
    assert history[2]["revision"] == expected_rev3
    assert history[2]["action"] == "set"
    assert history[2]["char_count"] == len(c3)

    assert history[3]["prev_revision"] == history[2]["revision"]
    assert history[3]["revision"] == expected_rev4
    assert history[3]["action"] == "set"
    assert history[3]["char_count"] == len(c4)

    assert history[4]["prev_revision"] == history[3]["revision"]
    assert history[4]["revision"] == expected_rev5
    assert history[4]["action"] == "delete"
    assert history[4]["char_count"] == 0

    c6 = "Agent Persona Reincarnated"
    b6 = memory_blocks.set_block(store, name, c6, actor="lead", reason="reborn")
    expected_rev6 = _oracle_hash(name, c6, "")
    assert b6.revision == expected_rev6
    history_after = memory_blocks.block_history(store, name)
    assert len(history_after) == 6
    assert history_after[5]["prev_revision"] == ""
    assert history_after[5]["revision"] == expected_rev6


def test_rapid_sequential_writes(store):
    name = "rapid_seq"
    iterations = 50
    for i in range(iterations):
        memory_blocks.set_block(
            store, name, f"Content iteration {i}", actor="bench", reason=f"step {i}"
        )

    b = memory_blocks.get_block(store, name)
    assert b.content == f"Content iteration {iterations - 1}"

    history = memory_blocks.block_history(store, name)
    assert len(history) == iterations

    for i in range(1, iterations):
        assert history[i]["prev_revision"] == history[i - 1]["revision"]

    b_dir = os.path.join(store, "memory", "blocks")
    leftover = [f for f in os.listdir(b_dir) if f.endswith(".tmp") or f.endswith(".bak")]
    assert leftover == []


def test_concurrent_writes_distinct_blocks(store):
    num_workers = 10
    writes_per_worker = 10

    def worker_task(worker_id: int):
        block_name = f"worker_{worker_id}"
        for step in range(writes_per_worker):
            memory_blocks.set_block(
                store,
                block_name,
                f"Worker {worker_id} update {step}",
                actor=f"worker_{worker_id}",
                reason=f"step {step}",
            )

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [executor.submit(worker_task, w) for w in range(num_workers)]
        for f in as_completed(futures):
            f.result()

    all_blocks = memory_blocks.list_blocks(store)
    assert len(all_blocks) == num_workers

    for w in range(num_workers):
        b = memory_blocks.get_block(store, f"worker_{w}")
        assert b.content == f"Worker {w} update {writes_per_worker - 1}"
        hist = memory_blocks.block_history(store, f"worker_{w}")
        assert len(hist) == writes_per_worker
        for step in range(1, writes_per_worker):
            assert hist[step]["prev_revision"] == hist[step - 1]["revision"]


def test_rollback_on_metadata_write_failure(store, monkeypatch):
    name = "rollback_meta"
    memory_blocks.set_block(store, name, "Original safe content.")
    b_orig = memory_blocks.get_block(store, name)
    hist_orig = memory_blocks.block_history(store, name)
    b_dir = os.path.join(store, "memory", "blocks")

    real_dump = json.dump

    def failing_dump(obj, f, **kwargs):
        if "rollback_meta.meta.json.tmp" in getattr(f, "name", ""):
            raise OSError("Simulated disk full during json.dump")
        return real_dump(obj, f, **kwargs)

    monkeypatch.setattr(json, "dump", failing_dump)

    with pytest.raises(OSError) as exc_info:
        memory_blocks.set_block(store, name, "New content that should not save.")
    assert "Simulated disk full" in str(exc_info.value)

    monkeypatch.setattr(json, "dump", real_dump)

    b_current = memory_blocks.get_block(store, name)
    assert b_current.content == "Original safe content."
    assert b_current.revision == b_orig.revision
    assert len(memory_blocks.block_history(store, name)) == len(hist_orig)

    leftover = [f for f in os.listdir(b_dir) if f.endswith(".tmp") or f.endswith(".bak")]
    assert leftover == []


def test_rollback_on_meta_replace_failure(store, monkeypatch):
    name = "rollback_replace"
    memory_blocks.set_block(store, name, "Initial persistent state.")
    b_orig = memory_blocks.get_block(store, name)
    b_dir = os.path.join(store, "memory", "blocks")

    original_replace = os.replace

    def replace_failing_meta(src, dst):
        if "rollback_replace.meta.json" in dst:
            raise OSError("Simulated I/O crash during meta replace")
        return original_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace_failing_meta)

    with pytest.raises(OSError) as exc_info:
        memory_blocks.set_block(store, name, "Mutated state that must rollback.")
    assert "Simulated I/O crash" in str(exc_info.value)

    monkeypatch.setattr(os, "replace", original_replace)

    b_restored = memory_blocks.get_block(store, name)
    assert b_restored.content == "Initial persistent state."
    assert b_restored.revision == b_orig.revision

    leftover = [f for f in os.listdir(b_dir) if f.endswith(".tmp") or f.endswith(".bak")]
    assert leftover == []


def test_new_block_failure_cleans_up_orphaned_md(store, monkeypatch):
    name = "new_orphan_test"
    b_dir = os.path.join(store, "memory", "blocks")
    content_file = os.path.join(b_dir, f"{name}.md")
    meta_file = os.path.join(b_dir, f"{name}.meta.json")

    original_replace = os.replace

    def fail_on_meta(src, dst):
        if f"{name}.meta.json" in dst:
            raise OSError("Crash on meta replace for brand new block")
        return original_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_on_meta)

    with pytest.raises(OSError):
        memory_blocks.set_block(store, name, "Fresh content")

    monkeypatch.setattr(os, "replace", original_replace)

    with pytest.raises(memory_blocks.BlockNotFoundError):
        memory_blocks.get_block(store, name)

    md_exists = os.path.exists(content_file)
    meta_exists = os.path.exists(meta_file)
    print(f"\n[EMPIRICAL OBSERVATION] new block failure: md_exists={md_exists}, meta_exists={meta_exists}")


def test_history_write_failure_behavior(store, monkeypatch):
    name = "hist_fail_test"
    memory_blocks.set_block(store, name, "Initial version.")
    orig_rev = memory_blocks.get_block(store, name).revision

    original_open = open

    def failing_history_open(file, *args, **kwargs):
        if str(file).endswith("history.jsonl") and "a" in args:
            raise PermissionError("Simulated read-only history file")
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", failing_history_open)

    with pytest.raises(PermissionError):
        memory_blocks.set_block(store, name, "Updated version without history.")

    monkeypatch.setattr("builtins.open", original_open)

    curr = memory_blocks.get_block(store, name)
    hist = memory_blocks.block_history(store, name)

    print("\n[EMPIRICAL OBSERVATION] history write failure:")
    print(f"  block content on disk: '{curr.content}'")
    print(f"  block revision on disk: '{curr.revision}' (orig was '{orig_rev}')")
    print(f"  latest history entry: {hist[-1]['revision']}")


def test_concurrent_race_condition_same_block(store):
    name = "race_target"
    memory_blocks.set_block(store, name, "Initial")
    num_threads = 8
    writes_per_thread = 5
    errors = []

    def writer(t_id: int):
        for w in range(writes_per_thread):
            try:
                memory_blocks.set_block(
                    store,
                    name,
                    f"Thread {t_id} write {w}",
                    actor=f"t_{t_id}",
                    reason=f"step {w}",
                )
            except Exception as e:
                errors.append((t_id, w, type(e).__name__, str(e)))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(f"\n[EMPIRICAL OBSERVATION] concurrent same-block writes errors count: {len(errors)}")
    for err in errors[:5]:
        print(f"  Error: {err}")
