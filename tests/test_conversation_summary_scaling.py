"""Summary reuse and bounded model context keep long sessions out of request RAM."""
import datetime as dt
import re

import pytest

from commontrace.conversation import Store
from commontrace.conversation import summary as summary_module
from commontrace.conversation.store import ConversationError
from commontrace.conversation.summary import (
    MAX_TRANSCRIPT_CHARS,
    MODEL_PAGE,
    PROMPT,
    ROLLING_PROMPT,
    _select_candidates,
    rolling,
    summarize,
    written,
)


def test_extractive_selection_preserves_recent_temporal_and_distinct_facts():
    rows = [
        {"weight": 10, "order": 0, "words": {"atlas", "office", "oslo"}, "dated": True},
        {"weight": 9, "order": 1, "words": {"atlas", "office", "oslo", "team"}, "dated": True},
        {"weight": 8, "order": 2, "words": {"atlas", "office", "paris", "changed"}, "dated": True},
        {"weight": 6, "order": 3, "words": {"team", "weekly", "reviews"}, "dated": False},
    ]
    selected = _select_candidates(rows, 3)
    assert rows[2] in selected  # later temporal update remains visible
    assert rows[3] in selected  # a distinct fact gets coverage beside repeated topic sentences


@pytest.mark.parametrize("sentences", [1, 3, 20])
def test_rolling_top_k_preserves_the_full_ranking_prefix(tmp_path, monkeypatch, sentences):
    with Store(str(tmp_path), "selection") as store:
        store.add("s", [{"text": f"Operational entry {i}: service {i % 7} deployed build {i % 13} in region {i % 4}."}
                        for i in range(120)], session_at="2026-10-01")
        turns = store.session_turns("s")
        select = summary_module._select_candidates
        legacy_state = {}
        with monkeypatch.context() as patch:
            patch.setattr(summary_module, "_select_candidates", lambda rows, _count: select(rows, len(rows)))
            expected = summary_module._fold_extractive(legacy_state, turns, sentences, 4000)
        observed = []

        def bounded(rows, count):
            observed.append(count)
            return select(rows, count)

        monkeypatch.setattr(summary_module, "_select_candidates", bounded)
        state = {}
        assert summary_module._fold_extractive(state, iter(turns), sentences, 4000) == expected
        assert state == legacy_state
        assert observed == [max(summary_module.ROLLING_POOL, sentences)]


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


def test_model_reads_all_source_pages_in_bounded_summary_passes(tmp_path, monkeypatch):
    with Store(str(tmp_path), "summary") as store:
        store.add("s", [{"text": f"Review entry {i}: " + "scheduled project review details " * 30}
                        for i in range(80)], session_at="2026-10-01")
        original = store.session_turns
        all_turns = original("s")
        pages, prompts = [], []

        def paged(session, **kwargs):
            assert kwargs["limit"] == MODEL_PAGE
            result = original(session, **kwargs)
            pages.append(len(result))
            return result

        def complete(prompt):
            prompts.append(prompt)
            last = max((int(n) for n in re.findall(r"Review entry (\d+):", prompt)), default=-1)
            return f"The project review included entry {last}.", {}

        monkeypatch.setattr(store, "session_turns", paged)
        assert summarize(store, ["s"], method="model", complete=complete)["summarized"] == 1
        assert len(prompts) > 1
        assert prompts[0].startswith("Summarise this conversation session")
        assert all(prompt.startswith(("Summarise this conversation session", "Update the running summary"))
                   for prompt in prompts)
        for prompt in prompts:
            start_marker = "\nNew messages:\n" if prompt.startswith("Update the running summary") else \
                "\n\nSession of 2026-10-01:\n"
            end_marker = "\n\nUpdated summary:" if prompt.startswith("Update the running summary") else "\n\nSummary:"
            transcript = prompt.split(start_marker, 1)[1].split(end_marker, 1)[0]
            assert len(transcript) <= MAX_TRANSCRIPT_CHARS
        assert max(pages) <= MODEL_PAGE and sum(pages) == len(all_turns)
        assert "entry 79" in store.summaries()["s"]["text"]
        assert store.summaries()["s"]["turns"] == len(all_turns)


def test_written_consumes_later_turns_after_a_full_context_chunk():
    class Source:
        at = dt.datetime(2026, 10, 1)
        speaker = "user"

        def annotated(self):
            return "x" * MAX_TRANSCRIPT_CHARS

    class Later(Source):
        def annotated(self):
            return "A later decision set the launch date to 2027-04-12."

    def sources():
        yield Source()
        yield Later()

    prompts = []

    def complete(prompt):
        prompts.append(prompt)
        return ("A summary with the launch date.", {})

    assert written(sources(), complete) == "A summary with the launch date."
    assert len(prompts) == 2
    assert all(len(prompt) <= MAX_TRANSCRIPT_CHARS + len(ROLLING_PROMPT.format(summary="", transcript=""))
               + len("A summary with the launch date.") for prompt in prompts)
    assert "2027-04-12" in prompts[1]
    assert prompts[0].startswith(PROMPT.split("{when}")[0])
    assert prompts[1].startswith("Update the running summary")


def test_rolling_model_summary_streams_and_folds_every_new_turn(tmp_path, monkeypatch):
    with Store(str(tmp_path), "rolling-summary") as store:
        store.add("s", [{"text": f"Rolling item {i}: " + "distinct session detail " * 45}
                        for i in range(60)], session_at="2026-10-01")
        original = store.session_turns
        pages = []
        prompts = []

        def paged(session, **kwargs):
            page = original(session, **kwargs)
            pages.append(len(page))
            return page

        def complete(prompt):
            prompts.append(prompt)
            last = max((int(n) for n in re.findall(r"Rolling item (\d+):", prompt)), default=-1)
            return f"Summary reaches item {last}.", {}

        monkeypatch.setattr(store, "session_turns", paged)
        result = rolling(store, "s", method="model", complete=complete)
        assert result["new_turns"] == result["turns"] == 60
        assert len(prompts) > 1 and max(pages) <= 256 and sum(pages) == 60
        assert "item 59" in result["text"]
        assert store.summaries()["s"]["turns"] == 60


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
