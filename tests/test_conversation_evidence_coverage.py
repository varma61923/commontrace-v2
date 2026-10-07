"""Real persisted recall must distinguish an object from its unrecorded identifier."""
from commontrace.conversation import Options, Store, recall
from commontrace.conversation.coverage import assess
from commontrace.recall import Item, pack


def test_identifier_requires_evidence_and_keeps_citable_context(tmp_path):
    root = str(tmp_path)
    options = Options(embedder=None, rerank=None, budget=600)
    with Store(root, "identifiers") as store:
        store.add("purchase", [{"speaker": "Nia", "text": "I bought a blue bicycle in Porto."}],
                  session_at="2025-01-01")
    with Store(root, "identifiers", create=False) as store:
        result = recall(store, "What is the serial number of Nia's bicycle?", options=options)
        assert result.explain["abstain"] is True
        assert result.explain["coverage"]["confidence"] == 0
        assert "bicycle" in result.context
        assert result.turns
        store.add("registration", [{"speaker": "Nia", "text": "The bicycle serial number is CT-492."}],
                  session_at="2025-02-01")
        recorded = recall(store, "What is the serial number of Nia's bicycle?", options=options)
        assert "CT-492" in recorded.context
        assert recorded.explain["coverage"]["abstain"] is False


def test_attribution_cannot_supply_missing_subject():
    result = assess("What does Nia remember about astronomy?", ["I live in Porto."], labels=["Nia astronomy"])
    assert result.abstain
    assert result.confidence == 0


def test_unrelated_substring_is_not_evidence():
    assert assess("What about the car?", ["The carpet is blue."]).abstain


def test_packing_retains_provenance_on_truncated_evidence():
    source = {"sources": ["trace:one"], "revision": "abc"}
    item = Item("facts", "fact:one", "Recorded evidence. " * 100, fused=1, provenance=source)
    packed = pack([item], 50)
    assert packed[0].truncated
    assert packed[0].to_dict()["provenance"] == source
