"""Gap bridging: the turns between two nearby hits of one session are delivered together."""
from commontrace.conversation import Options, Store, recall


def _store(tmp_path):
    s = Store(str(tmp_path), "agent")
    steps = ["Step 1 action: open the settings page",
             "Step 2 action: click the zebra toggle and the quokka button",
             "Step 3 action: scroll down", "Step 4 action: wait for the spinner", "Step 5 action: scroll up",
             "Step 6 action: press the zebra toggle and quokka button again",
             "Step 7 action: close the dialog", "Step 8 action: log out"]
    s.add("run", [{"speaker": "agent", "text": t} for t in steps], session_at="2024-01-01T10:00:00")
    s.add("other", [{"speaker": "agent", "text": f"Unrelated note {i} about lunch"} for i in range(30)],
          session_at="2024-01-02T10:00:00")
    return s


def test_bridging_fills_the_span_between_two_hits(tmp_path):
    store = _store(tmp_path)
    try:
        question = "Why did the agent press the zebra toggle and quokka button twice?"
        base = Options(budget=80, embedder=None, rerank=None, neighbours_before=0, neighbours_after=0)
        plain = recall(store, question, options=base)
        bridged = recall(store, question, options=Options(**{**base.__dict__, "bridge_turns": 4}))
        assert "Step 2" in plain.context and "Step 6" in plain.context
        assert "Step 4 action" not in plain.context and "Step 8 action" in plain.context
        for n in (2, 3, 4, 5, 6):
            assert f"Step {n} action" in bridged.context
        # The span is filled before a lower-ranked, unconnected hit, within the same budget.
        assert "Step 8 action" not in bridged.context and bridged.tokens <= 80
    finally:
        store.close()
