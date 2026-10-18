from collections import Counter

from commontrace import oblivious


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
    assert report["leakage"] == ["clusters_fetched", "cluster_count"]


def test_direct_server_response_reconstructs_selected_fixed_block():
    blocks = [b"ab", b"cd", b"ef"]
    q0, q1 = oblivious.split_query(2, 3, randomness=b"\x00")
    answer = oblivious.reconstruct(oblivious.server_answer(blocks, q0, block_bytes=2),
                                   oblivious.server_answer(blocks, q1, block_bytes=2))
    assert answer == b"ef"
