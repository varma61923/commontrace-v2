from collections import Counter

import pytest

from unimem import oblivious


def test_server_share_transcript_is_query_independent():
    size = 8
    # For each server, compare the same random-share distribution across targets.
    first_distributions, second_distributions = [], []
    for target in (1, 6):
        a, b = Counter(), Counter()
        for seed in range(2048):
            q0, q1 = oblivious.split_query(target, size, randomness=(seed % 256).to_bytes(1, "little"))
            a[q0] += 1
            b[q1] += 1
        first_distributions.append(a)
        second_distributions.append(b)
    assert first_distributions[0] == first_distributions[1]
    assert second_distributions[0] == second_distributions[1]


def test_clustered_xor_pir_matches_plaintext_ranking_and_clearance():
    centroids = {"near": [0.0, 0.0], "far": [10.0, 10.0]}
    records = [
        {"id": "best", "label": "public", "vector": [0.1, 0.1], "text": "best"},
        {"id": "next", "label": "internal", "vector": [0.2, 0.2], "text": "next"},
        {"id": "secret", "label": "restricted", "vector": [0.01, 0.01], "text": "secret"},
        {"id": "far", "label": "public", "vector": [9.8, 9.8], "text": "far"},
    ]
    database = oblivious.cluster_records(records, centroids, clearance="internal", block_bytes=512)
    server0 = oblivious.XorPIRReplica(database, block_bytes=512)
    server1 = oblivious.XorPIRReplica(database, block_bytes=512)

    def fetch(cluster, first_share, second_share):
        if first_share is None:
            return server0.handle(cluster, None)
        return server0.handle_many(cluster, first_share), server1.handle_many(cluster, second_share)

    report = oblivious.client_recall(centroids, [0.0, 0.0], clearance="internal", limit=3,
                                     clusters_to_fetch=2, fetch_pair=fetch)
    assert [row["id"] for row in report["results"]] == ["best", "next", "far"]
    assert all(row["label"] != "restricted" for row in report["results"])
    assert report["leakage"] == ["selected_cluster_ids", "clusters_fetched", "cluster_count"]


def test_direct_server_response_reconstructs_selected_fixed_block():
    blocks = [b"ab", b"cd", b"ef"]
    q0, q1 = oblivious.split_query(2, 3, randomness=b"\x00")
    answer = oblivious.reconstruct(oblivious.server_answer(blocks, q0, block_bytes=2),
                                   oblivious.server_answer(blocks, q1, block_bytes=2))
    assert answer == b"ef"


@pytest.mark.parametrize("metadata", [None, {}, {"count": -1, "block_bytes": 256},
    {"count": True, "block_bytes": 256}, {"count": 1, "block_bytes": 0},
    {"count": "1", "block_bytes": 256}, {"count": 1024, "block_bytes": 16384}])
def test_untrusted_metadata_is_rejected_before_fetching_blocks(metadata):
    requests = []

    def fetch(*args):
        requests.append(args)
        return metadata

    with pytest.raises(ValueError):
        oblivious.client_recall({"a": [0]}, [0], clearance="public", limit=1, fetch_pair=fetch)
    assert len(requests) == 1


@pytest.mark.parametrize("answers", [([], []), ([b"a" * 256], []),
    ([b"a"], [b"b"]), ([None], [b"b" * 256]), ([b"a" * 256], [b"b" * 256], [])])
def test_truncated_or_malformed_server_batches_never_become_partial_success(answers):
    def fetch(cluster, first, second):
        return {"count": 1, "block_bytes": 256} if first is None else answers

    with pytest.raises(ValueError):
        oblivious.client_recall({"a": [0]}, [0], clearance="public", limit=1, fetch_pair=fetch)


def test_pir_batch_has_a_combined_work_bound(monkeypatch):
    def unexpected_work(*args, **kwargs):
        pytest.fail("oversized batch reached the XOR loop")

    monkeypatch.setattr(oblivious, "server_answer", unexpected_work)
    with pytest.raises(ValueError, match="work budget"):
        oblivious.server_answer_many([b"a" * 256] * 1024, [bytes(128)] * 1024, block_bytes=256)


@pytest.mark.parametrize("vector", [[float("nan")], [float("inf")], [True], [10**1000], [], [0] * 4097])
def test_nonfinite_oversized_and_malformed_index_vectors_are_rejected(vector):
    with pytest.raises(ValueError):
        oblivious.cluster_records([{"id": "a", "label": "public", "vector": vector}],
                                   {"a": [0]}, clearance="public", block_bytes=256)


def test_replica_owns_its_fixed_size_database_snapshot():
    blocks = {"a": [b"ab", b"cd"]}
    replica = oblivious.XorPIRReplica(blocks, block_bytes=2)
    blocks["a"].clear()
    blocks.clear()
    assert replica.handle("a", None) == {"count": 2, "block_bytes": 2}
    assert replica.handle("a", b"\x01") == b"ab"


def test_corrupt_or_deeply_nested_blocks_raise_validation_errors():
    for block in (b"\xff", b"[" * 4000 + b"0" + b"]" * 4000):
        with pytest.raises(ValueError):
            oblivious._decode_block(block)


def test_duplicate_memory_ids_cannot_enter_the_pir_index():
    row = {"id": "same", "label": "public", "vector": [0]}
    with pytest.raises(ValueError, match="unique"):
        oblivious.cluster_records([row, row], {"a": [0]}, clearance="public", block_bytes=256)
