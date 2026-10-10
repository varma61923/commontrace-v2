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


def test_eventqa_is_left_unscored_rather_than_counted_as_a_miss(tmp_path):
    path = _write(tmp_path, [{"context": "Document 1: Debbie walked home.", "questions": ["What next?"],
                              "answers": [["Debbie waited."]], "metadata": {"source": "eventqa_full"}}])
    [(_space, _sessions, _now, [q])] = list(cb.mab_cases(path))
    assert q["answer_scorable"] is False
    assert cb.answer_in_any("Debbie waited.", q) is None


def test_longmemeval_style_contexts_become_dated_chat_sessions(tmp_path):
    history = [ "Chat Time: 2022/11/17 (Thu) 12:04", [{"role": "user", "content": "I adopted a cat named Miso."},
                                                    {"role": "assistant", "content": "Congratulations!"}],
                "Chat Time: 2022/12/01 (Thu) 09:00", [{"role": "user", "content": "Miso likes tuna."}]]
    path = _write(tmp_path, [{"context": repr(history), "questions": ["Cat's name?"], "answers": [["Miso"]],
                              "metadata": {"source": "longmemeval_s*"}}])
    [(space, sessions, _now, [q])] = list(cb.mab_cases(path))
    assert [(name, date, len(m)) for name, date, m in sessions] == [
        ("session-0", "2022/11/17 (Thu) 12:04", 2), ("session-1", "2022/12/01 (Thu) 09:00", 1)]
    assert sessions[0][2][1]["role"] == "assistant" and sessions[1][2][0]["id"] == f"{space}-s1-t0"
    assert q["type"] == "longmemeval" and q["answer_scorable"] is True


def test_conflict_resolution_facts_become_ordered_turns(tmp_path):
    context = "Here is a list of facts:\n0. Ana lives in Lima.\n1. Ana works at Acme.\n2. Ana lives in Quito.\n"
    path = _write(tmp_path, [{"context": context, "questions": ["Where does Ana live?"], "answers": [["Quito"]],
                              "metadata": {"source": "factconsolidation_sh_6k"}}])
    [(space, sessions, _now, [q])] = list(cb.mab_cases(path))
    [(_name, _date, messages)] = sessions
    assert [m["id"] for m in messages] == [f"{space}-f0", f"{space}-f1", f"{space}-f2"]
    assert messages[2]["text"] == "2. Ana lives in Quito." and q["type"] == "conflict-sh"
