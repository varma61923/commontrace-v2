"""Reference two-server XOR-PIR for label-filtered clustered memory blocks.

This deliberately small stdlib backend is an executable protocol reference,
not an ORAM, a verifiable PIR scheme, or protection against colluding servers.
"""
from __future__ import annotations

import json
import math
import secrets
from collections.abc import Sequence
from typing import Any, Protocol

from commontrace import frontier_protocol as fp

MAX_CLUSTERS = 256
MAX_BLOCKS_PER_CLUSTER = 1024
MAX_BLOCK_BYTES = 16_384
PUBLIC_CLUSTER_BLOCKS = 64


class FuturePIRBackend(Protocol):
    """Extension point for future ORAM-over-HNSW or verifiable-PIR adapters."""

    def retrieve(self, query: Any, *, clearance: str, limit: int) -> list[dict[str, Any]]: ...


class ORAMOverHNSWBackend(Protocol):
    """Future single-server ORAM-over-HNSW adapter (e.g. Compass-style)."""

    def retrieve(self, query_vector: list[float], *, clearance: str, limit: int) -> list[dict[str, Any]]: ...


class VerifiablePIRBackend(Protocol):
    """Future PIR adapter that returns a proof for independently verified answers."""

    def retrieve(self, query: Any, *, clearance: str, limit: int) -> dict[str, Any]: ...


def _distance(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        raise ValueError("vectors must be non-empty and have matching dimensions")
    return math.sqrt(sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)))


def cluster_records(records: list[dict[str, Any]], centroids: dict[str, list[float]], *, clearance: str,
                    labels: Sequence[str] = ("public", "internal", "confidential", "restricted"),
                    block_bytes: int = 4096, blocks_per_cluster: int = PUBLIC_CLUSTER_BLOCKS) -> dict[str, list[bytes]]:
    """Provision aligned fixed-size blocks after enforcing a maximum label."""
    if not centroids or len(centroids) > MAX_CLUSTERS:
        raise ValueError("centroid index must contain 1..256 clusters")
    block_bytes = fp.count(block_bytes, 256, MAX_BLOCK_BYTES)
    blocks_per_cluster = fp.count(blocks_per_cluster, 1, MAX_BLOCKS_PER_CLUSTER)
    if clearance not in labels or len(set(labels)) != len(labels):
        raise ValueError("clearance must be present in the ordered label hierarchy")
    ceiling = labels.index(clearance)
    vectors = {fp.text(name, "cluster name", 96): [fp.number(x, -1e6, 1e6) for x in vector]
               for name, vector in centroids.items()}
    if any(not v or len(v) > 4096 for v in vectors.values()) or len({len(v) for v in vectors.values()}) != 1:
        raise ValueError("centroid dimensions exceed bound")
    clusters: dict[str, list[bytes]] = {name: [] for name in sorted(vectors)}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("label"), str):
            raise ValueError("each memory block requires a label")
        if record["label"] not in labels:
            raise ValueError("memory block label is outside the configured hierarchy")
        if labels.index(record["label"]) > ceiling:
            continue
        vector = record.get("vector")
        if not isinstance(vector, list):
            raise ValueError("memory block requires an embedding vector")
        cluster = min(vectors, key=lambda name: (_distance(vector, vectors[name]), name))
        encoded = fp.wire({k: v for k, v in record.items() if k != "vector"} | {"vector": vector})
        if len(encoded) > block_bytes:
            raise ValueError("serialized memory exceeds the configured fixed block size")
        clusters[cluster].append(encoded.ljust(block_bytes, b"\0"))
    if any(len(blocks) > blocks_per_cluster for blocks in clusters.values()):
        raise ValueError("cluster exceeds the configured fixed public capacity")
    for blocks in clusters.values():
        blocks.extend([bytes(block_bytes)] * (blocks_per_cluster - len(blocks)))
    return clusters


def split_query(index: int, size: int, *, randomness: bytes | None = None) -> tuple[bytes, bytes]:
    """Return XOR query shares; either individual share is uniform and query-independent."""
    fp.count(size, 1, MAX_BLOCKS_PER_CLUSTER)
    fp.count(index, 0, size - 1)
    nbytes = (size + 7) // 8
    seed = randomness if randomness is not None else secrets.token_bytes(nbytes)
    if len(seed) != nbytes:
        raise ValueError("query-share randomness must have the exact bitmap size")
    first = bytearray(seed)
    second = bytearray(seed)
    second[index // 8] ^= 1 << (index % 8)
    return bytes(first), bytes(second)


def server_answer(blocks: list[bytes], share: bytes, *, block_bytes: int) -> bytes:
    """A server returns XOR of selected fixed-size blocks for its query share."""
    if not blocks or len(blocks) > MAX_BLOCKS_PER_CLUSTER or not 1 <= block_bytes <= MAX_BLOCK_BYTES:
        raise ValueError("invalid PIR database bounds")
    if len(share) != (len(blocks) + 7) // 8:
        raise ValueError("query share has an invalid bitmap length")
    output = bytearray(block_bytes)
    for idx, block in enumerate(blocks):
        if len(block) != block_bytes:
            raise ValueError("database blocks are not fixed-size")
        if share[idx // 8] & (1 << (idx % 8)):
            for position, byte in enumerate(block):
                output[position] ^= byte
    return bytes(output)


def server_answer_many(blocks: list[bytes], shares: list[bytes], *, block_bytes: int) -> list[bytes]:
    """Batch every block position into one response, avoiding a network RTT per item."""
    if len(shares) != len(blocks):
        raise ValueError("a PIR query share is required for every fixed cluster block")
    return [server_answer(blocks, share, block_bytes=block_bytes) for share in shares]


def reconstruct(first: bytes, second: bytes) -> bytes:
    if not first or len(first) != len(second):
        raise ValueError("PIR server responses must have equal non-zero length")
    return bytes(a ^ b for a, b in zip(first, second))


class XorPIRReplica:
    """Single-server endpoint adapter over one independently operated replica."""

    def __init__(self, clusters: dict[str, list[bytes]], *, block_bytes: int) -> None:
        self._clusters = clusters
        self._block_bytes = fp.count(block_bytes, 1, MAX_BLOCK_BYTES)

    def handle(self, cluster: str, share: bytes | None) -> dict[str, Any] | bytes:
        blocks = self._clusters.get(cluster)
        if blocks is None:
            raise KeyError("unknown public cluster")
        if share is None:
            return {"count": len(blocks), "block_bytes": self._block_bytes}
        return server_answer(blocks, share, block_bytes=self._block_bytes)

    def handle_many(self, cluster: str, shares: list[bytes]) -> list[bytes]:
        blocks = self._clusters.get(cluster)
        if blocks is None:
            raise KeyError("unknown public cluster")
        return server_answer_many(blocks, shares, block_bytes=self._block_bytes)


def _decode_block(block: bytes) -> dict[str, Any] | None:
    value = block.rstrip(b"\0")
    if not value:
        return None
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("PIR block is not a memory object")
    return decoded


def client_recall(centroids: dict[str, list[float]], query_vector: list[float], *, clearance: str, limit: int,
                  fetch_pair: Any, clusters_to_fetch: int | None = None,
                  labels: Sequence[str] = ("public", "internal", "confidential", "restricted")) -> dict[str, Any]:
    """Select public-centroid clusters locally, PIR-fetch their blocks, rerank locally.

    fetch_pair(cluster, shares0, shares1) sends all fixed-capacity shares to each
    non-colluding server in one request. Only cluster and public capacity
    leakage are claimed; timing and transport metadata are deployment concerns.
    """
    limit = fp.count(limit, 1, 100)
    if not centroids or len(centroids) > MAX_CLUSTERS:
        raise ValueError("centroid index must contain 1..256 clusters")
    query_vector = [fp.number(x, -1e6, 1e6) for x in query_vector]
    fp.count(len(query_vector), 1, 4096, "query dimensions")
    centroids = {fp.text(name, "cluster name", 96): [fp.number(x, -1e6, 1e6) for x in vector]
                 for name, vector in centroids.items()}
    count = (len(centroids) if clusters_to_fetch is None else
             fp.count(clusters_to_fetch, 1, min(MAX_CLUSTERS, len(centroids))))
    selected = sorted(centroids, key=lambda name: (_distance(query_vector, centroids[name]), name))[:count]
    found = []
    blocks_fetched = 0
    for cluster in selected:
        metadata = fetch_pair(cluster, None, None)
        size, block_bytes = int(metadata["count"]), int(metadata["block_bytes"])
        if size == 0:
            continue
        if size > MAX_BLOCKS_PER_CLUSTER or block_bytes > MAX_BLOCK_BYTES:
            raise ValueError("PIR server metadata exceeds protocol limits")
        query_shares = [split_query(index, size) for index in range(size)]
        first_shares = [pair[0] for pair in query_shares]
        second_shares = [pair[1] for pair in query_shares]
        first_answers, second_answers = fetch_pair(cluster, first_shares, second_shares)
        for first, second in zip(first_answers, second_answers):
            record = _decode_block(reconstruct(first, second))
            if record is not None:
                if clearance not in labels or record.get("label") not in labels:
                    raise ValueError("server returned a label outside the configured hierarchy")
                if labels.index(record["label"]) > labels.index(clearance):
                    raise ValueError("server returned content above the requested clearance")
                found.append(record)
            blocks_fetched += 1
    ranked = sorted(found, key=lambda row: (_distance(query_vector, row["vector"]), str(row.get("id", ""))))
    return {"results": ranked[:limit], "clusters_fetched": len(selected), "cluster_count": len(centroids),
            "blocks_fetched": blocks_fetched, "leakage": ["clusters_fetched", "cluster_count"],
            "control_loop_use": False}
