"""MemoryAgentBench loader: documents as messages, every accepted answer checked."""
import pytest

from benchmarks import conversation_bench as cb

pd = pytest.importorskip("pandas")
pytest.importorskip("pyarrow")


def _write(tmp_path, rows):
    path = tmp_path / "mab.parquet"
    pd.DataFrame(rows).to_parquet(path)
    return str(path)


def test_mab_splits_documents_and_keeps_all_accepted_answers(tmp_path):
    context = "Document 1: Normandy is in France.\nDocument 2: The Loire is a river.\nDocument 3: Paris."
    path = _write(tmp_path, [{"context": context, "questions": ["Where is Normandy?"],
                              "answers": [["France", "French Republic"]],
                              "metadata": {"source": "ruler_qa1_197K", "qa_pair_ids": ["p0"]}}])
    [(space, sessions, now, questions)] = list(cb.mab_cases(path))
    messages = sessions[0][2]
    assert [m["text"].split(":")[0] for m in messages] == ["Document 1", "Document 2", "Document 3"]
    assert now is None and space == "mab-ruler_qa1_197K-0"
    [q] = questions
    assert q["id"] == f"{space}-p0" and q["type"] == "ruler" and q["evidence"] == set()
    assert q["answers"] == ["France", "French Republic"]
    assert cb.answer_in_any("It is in the French Republic.", q) is True
    assert cb.answer_in_any("It is in Spain.", q) is False


def test_mab_chunks_contexts_without_document_markers(tmp_path):
    context = "word " * (cb.MAB_CHUNK_CHARS // 2)
    path = _write(tmp_path, [{"context": context, "questions": ["q"], "answers": [["a"]],
                              "metadata": {"source": "eventqa_full"}}] * 3)
    cases = list(cb.mab_cases(path, limit=2))
    assert len(cases) == 2
    messages = cases[0][1][0][2]
    assert len(messages) == 3 and all(len(m["text"]) <= cb.MAB_CHUNK_CHARS for m in messages)


def test_mab_cases_round_trip_through_the_chunk_payload(tmp_path):
    path = _write(tmp_path, [{"context": "Document 1: x\nDocument 2: y", "questions": ["q"],
                              "answers": [["x", "y"]], "metadata": {"source": "s"}}])
    cases = list(cb.mab_cases(path))
    assert cb._cases_from_payload(cb._payload_from_cases(cases)) == cases
