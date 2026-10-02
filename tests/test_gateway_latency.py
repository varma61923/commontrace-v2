from commons.eval import gateway_latency


def test_recall_stays_fast_and_flat_as_the_log_grows():
    result = gateway_latency.measure(occasions=4000, items=8, durable=False)
    first, last = result["buckets"][0], result["buckets"][-1]
    assert last["recall_p50_ms"] < 25 and last["outcome_p50_ms"] < 25
    assert last["recall_p50_ms"] < first["recall_p50_ms"] * 3 + 1.0
