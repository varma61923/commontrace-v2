"""A loose regression guard on the control-loop path; the real numbers come from
`python -m commons.eval.gateway_latency`, and are this machine's."""
from commons.eval import gateway_latency


def test_recall_stays_fast_and_flat_as_the_log_grows():
    result = gateway_latency.measure(occasions=4000, items=8, durable=False)
    first, last = result["buckets"][0], result["buckets"][-1]
    # Absurdly loose on absolute time (CI machines vary); the shape is the assertion.
    assert last["recall_p50_ms"] < 25 and last["outcome_p50_ms"] < 25
    assert last["recall_p50_ms"] < first["recall_p50_ms"] * 3 + 1.0   # no growth with history
