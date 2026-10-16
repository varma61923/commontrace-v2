from __future__ import annotations

import hashlib
import json

import pytest

from commontrace import frontier_protocol as fp
from commontrace import transparency_log as log


def test_rfc_golden_vectors():
    assert log.EMPTY == hashlib.sha256(b"").hexdigest()
    assert log.leaf_hash({}) == hashlib.sha256(b"\x00{}").hexdigest()
    vector = json.loads(open("protocol/conformance/transparency.json").read())
    assert log.leaf_hash(vector["statement"]) == vector["leaf_hash"]
    assert log.node_hash(vector["leaf_hash"], vector["leaf_hash"]) == vector["node_hash"]
    # Interoperability with the RFC 6962 reference tree (raw-byte leaves).
    reference = vector["rfc6962"]
    hashes = [hashlib.sha256(b"\x00" + bytes.fromhex(leaf)).hexdigest() for leaf in reference["leaves_hex"]]
    assert log.prefix_roots(hashes)[1:] == reference["roots"]
    assert reference["roots"][-1] == "5dc9da79a70659a9ad559cb701ded9a2ab9d823aad2f4960cfe370eff4604328"
    for case in reference["inclusion"]:
        root = reference["roots"][case["size"] - 1]
        assert log.inclusion(hashes[:case["size"]], case["index"]) == case["proof"]
        assert log.verify_inclusion(hashes[case["index"]], case["index"], case["size"], case["proof"], root)
    for case in reference["consistency"]:
        old, new = reference["roots"][case["from"] - 1], reference["roots"][case["to"] - 1]
        assert log.verify_consistency(case["from"], case["to"], old, new, case["proof"])
    frontier: list = []
    for index, leaf in enumerate(hashes):
        frontier, proof = log.frontier_append_with_proof(frontier, leaf)
        assert proof == log.inclusion(hashes[:index + 1], index)
        assert log.frontier_root(frontier) == reference["roots"][index]


def test_leaf_only_appends_are_covered_by_the_next_head(tmp_path):
    root = str(tmp_path)
    leaves = [log.append(root, "holdout-outcome", {"n": i}, receipt=False) for i in range(5)]
    assert all("head" not in leaf for leaf in leaves)
    assert not fp.journal(root, log.HEADS)  # no signed checkpoint per hot-path leaf
    for leaf in leaves:
        receipt = log.prove(root, leaf["leaf"])
        assert receipt["head"]["record"]["tree_size"] == 5 and log.verify(root, receipt)


def test_all_proof_shapes_and_tampering():
    hashes = [log.leaf_hash({"i": i}) for i in range(40)]
    for size in range(1, 41):
        current = hashes[:size]
        root = log.tree_hash(current)
        for index in range(size):
            proof = log.inclusion(current, index)
            assert log.verify_inclusion(current[index], index, size, proof, root)
            assert not log.verify_inclusion("00" * 32, index, size, proof, root)
            assert not log.verify_inclusion(current[index], index, size, proof + ["00" * 32], root)
        for old_size in range(size + 1):
            proof = log.consistency(current, old_size)
            old_root = log.tree_hash(current[:old_size])
            assert log.verify_consistency(old_size, size, old_root, root, proof)
            if old_size:
                assert not log.verify_consistency(old_size, size, "00" * 32, root, proof)


def test_signed_history_and_pinned_checkpoint(tmp_path):
    root = str(tmp_path)
    first = log.append(root, "write", {"id": "a"})
    assert log.verify(root, first)
    log.append(root, "revoke", {"id": "a"})
    proof = log.consistent(root, 1)
    assert log.verify_consistency(1, 2, first["head"]["record"]["root_hash"], proof["root_hash"], proof["proof"])
    assert log.verify(root, log.prove(root, 0))
    # Roll back leaves while retaining a checkpoint: fail closed.
    path = tmp_path / "memory" / log.LEAVES
    path.write_text(path.read_text().splitlines()[0] + "\n")
    with pytest.raises(ValueError, match="truncated"):
        log.head(root)


def test_reorder_fork_and_cross_store_signature(tmp_path):
    first, second = str(tmp_path / "first"), str(tmp_path / "second")
    receipt = log.append(first, "write", {"id": "a"})
    log.append(first, "write", {"id": "b"})
    log.append(second, "write", {"id": "different"})
    assert not log.verify(second, receipt)
    leaves = fp.rows(fp.filename(first, log.LEAVES))
    leaves.reverse()
    from commontrace import _jsonl
    _jsonl.write_rows(fp.filename(first, log.LEAVES), leaves)
    with pytest.raises(ValueError):
        log.head(first)


def test_log_cli_and_mcp(tmp_path):
    from commontrace.mcp_server import build_server
    pytest.importorskip("mcp")
    from tests.test_mcp_server import call, cli

    root = str(tmp_path)
    receipt = log.append(root, "write", {"id": "a"})
    filename = tmp_path / "receipt.json"
    filename.write_text(json.dumps(receipt))
    result = cli("log", "verify", "--receipt", str(filename), "--dest", root)
    assert result.returncode == 0, result.stderr
    assert call(build_server(root), "log_verify", receipt=receipt)["valid"]


def test_optional_ed25519(tmp_path):
    pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    from commontrace import memory_authority
    root = str(tmp_path)
    memory_authority.configure_signing(root)
    receipt = log.append(root, "write", {"id": "public"})
    assert receipt["head"]["algorithm"] == "ed25519"
    assert log.verify(root, receipt)


def test_verified_cache_detects_edits_and_has_bounded_append_work(tmp_path, monkeypatch):
    root = str(tmp_path)
    log.append(root, "write", {"id": "first"})
    original, calls = fp.verify, []
    def counted(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(fp, "verify", counted)
    for i in range(40):
        log.append(root, "write", {"id": str(i)})
    assert len(calls) < 10  # No complete-history signature scan per append.
    path = tmp_path / "memory" / log.LEAVES
    rows = path.read_text().splitlines()
    forged = json.loads(rows[0])
    forged["record"]["statement"]["record"]["event"] = "forged"
    rows[0] = json.dumps(forged)
    path.write_text("\n".join(rows) + "\n")
    with pytest.raises(ValueError):
        log.head(root)


def test_read_only_state_and_returned_values_cannot_poison_cache(tmp_path):
    from commontrace import lineage
    absent = tmp_path / "absent"
    assert lineage.labels(str(absent), "old")["integrity"] == "unknown"
    assert not absent.exists()
    root = str(tmp_path)
    receipt = log.append(root, "write", {"id": "first"})
    receipt["head"]["record"]["root_hash"] = "00" * 32
    assert log.head(root)["record"]["root_hash"] != "00" * 32
    returned = fp.journal(root, log.LEAVES)
    returned[0]["statement"]["record"]["event"] = "caller mutation"
    assert fp.journal(root, log.LEAVES)[0]["statement"]["record"]["event"] == "write"


def test_concurrent_appends_keep_one_history(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    root = str(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda i: log.append(root, "write", {"id": str(i)}), range(24)))
    assert sorted(r["head"]["record"]["tree_size"] for r in receipts) == list(range(1, 25))
    assert all(log.verify(root, receipt) for receipt in receipts)


def test_signed_json_detaches_aliases_before_untrusted_caller_mutates(tmp_path):
    shared = {"steps": ["check the owner"]}
    supplied = {"first": shared, "second": shared}
    signed = fp.sign(str(tmp_path), supplied)
    shared["steps"][0] = "bypass validation"
    assert signed["record"]["first"]["steps"] == ["check the owner"]
    assert signed["record"]["first"] is not signed["record"]["second"]
    assert fp.verify(str(tmp_path), signed)
