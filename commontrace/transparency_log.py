"""RFC 6962/9162 Merkle receipts and append-only history; SCITT-style envelopes.

Leaf H = SHA256(0x00 || canonical signed statement), node H = SHA256(0x01 ||
left || right). Inclusion authenticates membership, not statement truth.
Consistency detects equivocation only against a previously pinned or witnessed
head. No private log can prevent an operator from hiding an entire history from
an observer with no checkpoint. HMAC heads require a shared verifier secret;
use the security extra and pinned Ed25519 keys for public verification.
"""
from __future__ import annotations

import hashlib
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Sequence

from commontrace import _jsonl
from commontrace import frontier_protocol as fp

LEAVES = "transparency-leaves.jsonl"
HEADS = "transparency-heads.jsonl"
EMPTY = hashlib.sha256(b"").hexdigest()


@dataclass
class _Tree:
    leaf_tip: tuple[int, str]
    head_tip: tuple[int, str]
    hashes: list[str]
    roots: list[str]
    nodes: dict[tuple[int, int], str] = field(default_factory=dict)


_TREES: OrderedDict[str, _Tree] = OrderedDict()


def _remember(root: str, state: _Tree) -> None:
    _TREES[root] = state
    _TREES.move_to_end(root)
    while len(_TREES) > 8 or sum(len(s.hashes) * 256 + len(s.nodes) * 512
                               for s in _TREES.values()) > 128 * fp.MAX_BYTES:
        _TREES.popitem(last=False)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_TREES.clear)


def _range(state: _Tree, start: int, size: int) -> str:
    if not size:
        return EMPTY
    key = start, size
    if key in state.nodes:
        return state.nodes[key]
    if size == 1:
        result = state.hashes[start]
    else:
        split = _split(size)
        result = node_hash(_range(state, start, split), _range(state, start + split, size - split))
    if size & (size - 1) == 0:
        state.nodes[key] = result
    return result


def _proof(state: _Tree, index: int, start: int = 0, size: int | None = None) -> list[str]:
    size = len(state.hashes) if size is None else size
    if size == 1:
        return []
    split = _split(size)
    if index < start + split:
        return _proof(state, index, start, split) + [_range(state, start + split, size - split)]
    return _proof(state, index, start + split, size - split) + [_range(state, start, split)]


def leaf_hash(statement: dict[str, Any]) -> str:
    return hashlib.sha256(b"\x00" + fp.wire(statement)).hexdigest()


def node_hash(left: str, right: str) -> str:
    if len(left) != 64 or len(right) != 64:
        raise ValueError("Merkle hashes must be 32 bytes")
    return hashlib.sha256(b"\x01" + bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()


def _split(size: int) -> int:
    return 1 << ((size - 1).bit_length() - 1)


def tree_hash(hashes: Sequence[str]) -> str:
    fp.count(len(hashes), 0, fp.MAX_ROWS)
    if not hashes:
        return EMPTY
    if len(hashes) == 1:
        return hashes[0]
    split = _split(len(hashes))
    return node_hash(tree_hash(hashes[:split]), tree_hash(hashes[split:]))


def frontier_append(frontier: Sequence[str | None], leaf: str) -> list[str | None]:
    """Add one leaf to a Merkle frontier (one perfect-subtree root per set bit of the size).

    ``frontier[level]`` holds the root of the 2**level-leaf subtree, or None.
    Appending is a binary increment: O(log n) hashes, independent of history.
    """
    result = list(frontier)
    value, level = leaf, 0
    while level < len(result) and result[level] is not None:
        value = node_hash(str(result[level]), value)
        result[level] = None
        level += 1
    if level == len(result):
        result.append(value)
    else:
        result[level] = value
    return result


def frontier_append_with_proof(frontier: Sequence[str | None], leaf: str) -> tuple[list[str | None], list[str]]:
    """`frontier_append` plus the new (rightmost) leaf's RFC 6962 inclusion proof, in O(log n).

    The subtrees merged by the carry are the leaf's left siblings, bottom-up;
    above its subtree, the remaining smaller-to-larger frontier roots complete the path.
    """
    result = list(frontier)
    siblings: list[str] = []
    value, level = leaf, 0
    while level < len(result) and result[level] is not None:
        sibling = str(result[level])
        siblings.append(sibling)
        value = node_hash(sibling, value)
        result[level] = None
        level += 1
    if level == len(result):
        result.append(value)
    else:
        result[level] = value
    siblings.extend(str(subtree) for subtree in result[level + 1:] if subtree is not None)
    return result, siblings


def frontier_root(frontier: Sequence[str | None]) -> str:
    """RFC 6962 root of the tree a frontier summarizes (smaller subtrees sit to the right)."""
    accumulator = ""
    for subtree in frontier:
        if subtree is not None:
            accumulator = node_hash(subtree, accumulator) if accumulator else subtree
    return accumulator or EMPTY


def frontier_size(frontier: Sequence[str | None]) -> int:
    return sum(1 << level for level, subtree in enumerate(frontier) if subtree is not None)


def prefix_roots(hashes: Sequence[str]) -> list[str]:
    """All checkpoint roots in O(n log n), with an O(log n) Merkle frontier."""
    frontier: list[str | None] = []
    roots = [EMPTY]
    for leaf in hashes:
        frontier = frontier_append(frontier, leaf)
        roots.append(frontier_root(frontier))
    return roots


def inclusion(hashes: Sequence[str], index: int) -> list[str]:
    fp.count(index, 0, len(hashes) - 1, "leaf index")
    if len(hashes) == 1:
        return []
    split = _split(len(hashes))
    if index < split:
        return inclusion(hashes[:split], index) + [tree_hash(hashes[split:])]
    return inclusion(hashes[split:], index - split) + [tree_hash(hashes[:split])]


def verify_inclusion(leaf: str, index: int, size: int, proof: list[str], root_hash: str) -> bool:
    try:
        fp.count(size, 1, fp.MAX_ROWS)
        fp.count(index, 0, size - 1)
        fp.count(len(proof), 0, 32)
        def fold(start: int, length: int, offset: int) -> tuple[str, int]:
            if length == 1:
                return leaf, offset
            split = _split(length)
            if index < start + split:
                child, used = fold(start, split, offset)
                return node_hash(child, proof[used]), used + 1
            child, used = fold(start + split, length - split, offset)
            return node_hash(proof[used], child), used + 1
        computed, used = fold(0, size, 0)
        return computed == root_hash and used == len(proof)
    except (ValueError, IndexError, TypeError):
        return False


def consistency(hashes: Sequence[str], old_size: int) -> list[str]:
    fp.count(old_size, 0, len(hashes), "old tree size")
    if old_size == 0:
        return []

    def subproof(items: Sequence[str], size: int, complete: bool) -> list[str]:
        if size == len(items):
            return [] if complete else [tree_hash(items)]
        split = _split(len(items))
        if size <= split:
            return subproof(items[:split], size, complete) + [tree_hash(items[split:])]
        return subproof(items[split:], size - split, False) + [tree_hash(items[:split])]

    return subproof(hashes, old_size, True)


def verify_consistency(old_size: int, size: int, old_root: str, root_hash: str, proof: list[str]) -> bool:
    try:
        fp.count(size, 0, fp.MAX_ROWS)
        fp.count(old_size, 0, size)
        fp.count(len(proof), 0, 64)
        if old_size == 0:
            return old_root == EMPTY and not proof
        if old_size == size:
            return old_root == root_hash and not proof
        first, second = old_size - 1, size - 1
        while first & 1:
            first >>= 1
            second >>= 1
        offset = 0
        if first == 0:
            old_acc = new_acc = old_root
        else:
            old_acc = new_acc = proof[0]
            offset = 1
        for sibling in proof[offset:]:
            if second == 0:
                return False
            if first & 1 or first == second:
                old_acc, new_acc = node_hash(sibling, old_acc), node_hash(sibling, new_acc)
                while first and not first & 1:
                    first >>= 1
                    second >>= 1
            else:
                new_acc = node_hash(new_acc, sibling)
            first >>= 1
            second >>= 1
        return second == 0 and old_acc == old_root and new_acc == root_hash
    except (ValueError, IndexError, TypeError):
        return False


def _tree(root: str) -> _Tree:
    root = os.path.realpath(root)
    leaf_tip, head_tip = fp.checkpoint(root, LEAVES), fp.checkpoint(root, HEADS)
    cached = _TREES.get(root)
    if cached and cached.leaf_tip == leaf_tip and cached.head_tip == head_tip:
        _TREES.move_to_end(root)
        return cached
    records = fp.journal(root, LEAVES)
    result = []
    for row in records:
        if not fp.verify(root, row["statement"]):
            raise ValueError("invalid transparency statement signature")
        result.append(leaf_hash(row["statement"]))
    roots = prefix_roots(result)
    for head in fp.journal(root, HEADS):
        size = head["tree_size"]
        if size > len(result) or roots[size] != head["root_hash"]:
            raise ValueError("transparency history was truncated or forked")
    state = _Tree(leaf_tip, head_tip, result, roots)
    _remember(root, state)
    return state


def _hashes(root: str) -> list[str]:
    return list(_tree(root).hashes)


def head(root: str, *, durable: bool = True) -> dict[str, Any]:
    with _jsonl.locked(fp.filename(root, LEAVES)), fp._GUARD:
        state = _tree(root)
        if state.head_tip[0]:
            latest = fp.signed_at(root, HEADS, state.head_tip[0] - 1)
            if latest["record"]["tree_size"] == len(state.hashes):
                return latest
        signed = fp.append(root, HEADS, {"tree_size": len(state.hashes), "root_hash": state.roots[-1],
                                        "log_id": "local-store", "signature_kind": "store-pinned"}, durable=durable)
        state.head_tip = fp.checkpoint(root, HEADS)
        _remember(os.path.realpath(root), state)
        # The node cache is disposable: verification always recomputes from leaves.
        _jsonl.write_json(fp.filename(root, "transparency-nodes.json"),
                          {"tree_size": len(state.hashes), "root_hash": state.roots[-1]})
        return signed


def append(root: str, event: str, statement: dict[str, Any], *, durable: bool = True,
           receipt: bool = True) -> dict[str, Any]:
    """Commit a signed leaf; with ``receipt`` also sign a head and return an inclusion proof.

    Hot paths (holdout assignments, outcomes, ordinary writes) pass
    ``receipt=False``: the leaf is committed and covered by the next signed head,
    which `head`, `prove` or any receipt-bearing append publishes on demand.
    Skipping the per-leaf head saves a signed, fsynced checkpoint per write
    without weakening tamper evidence: heads are always recomputed from leaves.
    """
    fp.text(event, "event", 128)
    with _jsonl.locked(fp.filename(root, LEAVES)), fp._GUARD:
        state = _tree(root)
        signed = fp.sign(root, {"event": event, "statement": statement, "created_at": fp.now()}, durable=durable)
        fp.append(root, LEAVES, {"statement": signed}, durable=durable)
        state.hashes.append(leaf_hash(signed))
        state.roots.append(_range(state, 0, len(state.hashes)))
        state.leaf_tip = fp.checkpoint(root, LEAVES)
        _remember(os.path.realpath(root), state)
        if not receipt:
            return {"leaf": len(state.hashes) - 1, "statement": signed}
        checkpoint = head(root, durable=durable)
        return {"leaf": len(state.hashes) - 1, "statement": signed, "head": checkpoint,
                "proof": _proof(state, len(state.hashes) - 1)}


def prove(root: str, leaf: int) -> dict[str, Any]:
    with _jsonl.locked(fp.filename(root, LEAVES)), fp._GUARD:
        state = _tree(root)
        fp.count(leaf, 0, len(state.hashes) - 1, "leaf")
        return {"leaf": leaf, "statement": fp.signed_at(root, LEAVES, leaf)["record"]["statement"],
                "head": head(root), "proof": _proof(state, leaf)}


def verify(root: str, receipt: dict[str, Any]) -> bool:
    try:
        checkpoint = receipt["head"]
        body = checkpoint["record"]
        return (fp.verify(root, checkpoint) and fp.verify(root, receipt["statement"])
                and verify_inclusion(leaf_hash(receipt["statement"]), receipt["leaf"], body["tree_size"],
                                     receipt["proof"], body["root_hash"]))
    except (KeyError, ValueError, TypeError):
        return False


def consistent(root: str, old_size: int, size: int | None = None) -> dict[str, Any]:
    with _jsonl.locked(fp.filename(root, LEAVES)), fp._GUARD:
        hashes = _hashes(root)
        size = len(hashes) if size is None else fp.count(size, 0, len(hashes), "tree size")
        hashes = hashes[:size]
        fp.count(old_size, 0, size, "old tree size")
        return {"from": old_size, "to": size, "old_root": tree_hash(hashes[:old_size]),
                "root_hash": tree_hash(hashes), "proof": consistency(hashes, old_size)}
