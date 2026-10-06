"""Summary reuse and bounded model context keep long sessions out of request RAM."""
import datetime as dt

import pytest

from commontrace.conversation import Store
from commontrace.conversation.store import ConversationError
from commontrace.conversation.summary import MAX_TRANSCRIPT_CHARS, MODEL_PAGE, PROMPT, summarize, written


def test_unchanged_summary_never_hydrates_source_messages(tmp_path, monkeypatch):
    with Store(str(tmp_path), "summary") as store:
        store.add("s", [{"text": "The launch venue is Oslo."}])
        store.set_summary("s", "The launch venue is Oslo.", "extractive")

        def forbidden(*args, **kwargs):
            raise AssertionError("valid summary must not read raw session messages")

        monkeypatch.setattr(store, "session_turns", forbidden)
        assert summarize(store) == {
            "space": "summary", "summarized": 0, "unchanged": 1, "method": "extractive",
        }
        assert summarize(store, ["s"], method="model", complete=forbidden)["unchanged"] == 1


def test_forced_and_changed_summaries_still_use_current_sources(tmp_path):
    with Store(str(tmp_path), "summary") as store:
        store.add("s", [{"text": "The launch venue is Oslo."}])
        store.set_summary("s", "Original venue.", "extractive")
        assert summarize(store, ["s"], force=True)["summarized"] == 1
        store.add("s", [{"text": "The revised launch venue is Paris."}])
        assert summarize(store, ["s"])["summarized"] == 1
        assert "Paris" in store.summaries()["s"]["text"]
        with pytest.raises(ConversationError, match="no session"):
            summarize(store, ["absent"])


def test_model_reads_bounded_source_pages_and_preserves_exact_prompt(tmp_path, monkeypatch):
    with Store(str(tmp_path), "summary") as store:
        store.add("s", [{"text": f"Review entry {i}: " + "scheduled project review details " * 30}
                        for i in range(80)], session_at="2026-10-01")
        original = store.session_turns
        all_turns = original("s")
        expected = PROMPT.format(
            when="2026-10-01",
            transcript="\n".join(f"{t.speaker}: {t.annotated()}" for t in all_turns)[:MAX_TRANSCRIPT_CHARS],
        )
        pages, prompts = [], []

        def paged(session, **kwargs):
            assert kwargs["limit"] == MODEL_PAGE
            result = original(session, **kwargs)
            pages.append(len(result))
            return result

        def complete(prompt):
            prompts.append(prompt)
            return "The project review was scheduled.", {}

        monkeypatch.setattr(store, "session_turns", paged)
        assert summarize(store, ["s"], method="model", complete=complete)["summarized"] == 1
        assert prompts == [expected]
        assert max(pages) <= MODEL_PAGE and sum(pages) < len(all_turns)
        assert store.summaries()["s"]["turns"] == len(all_turns)


def test_written_stops_consuming_source_after_exact_context_limit():
    class Source:
        at = dt.datetime(2026, 10, 1)
        speaker = "user"

        def annotated(self):
            return "x" * MAX_TRANSCRIPT_CHARS

    def sources():
        yield Source()
        raise AssertionError("source after the model context must not be hydrated")

    prompts = []

    def complete(prompt):
        prompts.append(prompt)
        return "A summary.", {}

    assert written(sources(), complete) == "A summary."
    assert prompts == [PROMPT.format(when="2026-10-01", transcript=("user: " + "x" * MAX_TRANSCRIPT_CHARS)
                                   [:MAX_TRANSCRIPT_CHARS])]


def test_concurrent_append_cannot_certify_a_partial_summary(tmp_path):
    with Store(str(tmp_path), "summary") as store, Store(str(tmp_path), "summary") as other:
        store.add("s", [{"text": "The launch venue is Oslo."}])

        def changing_model(prompt):
            other.add("s", [{"text": "The launch venue changed to Paris."}])
            return "The launch venue is Oslo.", {}

        with pytest.raises(ConversationError, match="evidence changed during summarization"):
            summarize(store, ["s"], method="model", complete=changing_model)
        assert not store.summaries()
        assert store.stats()["turns"] == 2
        assert summarize(store, ["s"])["summarized"] == 1
        assert "Paris" in store.summaries()["s"]["text"]
