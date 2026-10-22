"""BEAM loading: the 100K-1M session layout and the 10M plan/batch/turn layout."""
from __future__ import annotations

import pytest

from benchmarks.conversation_bench import _beam_sessions, _ids, beam_cases

pd = pytest.importorskip("pandas")
pytest.importorskip("pyarrow")


def _message(i, text, anchor=None, role="user"):
    return {"content": text, "id": i, "index": f"1,{i}", "question_type": "main_question", "role": role,
            "time_anchor": anchor}


def _questions(ids):
    return {"information_extraction": [{"question": "What port?", "answer": "5432", "source_chat_ids": ids,
                                        "rubric": ["States port 5432"]}],
            "abstention": [{"question": "Who is Johnny?", "answer": "Not mentioned", "rubric": ["Abstains"]}]}


def test_ids_accept_lists_dicts_and_the_10m_text_encoding():
    assert _ids([1, 2]) == {"1", "2"}
    assert _ids({"first": [3], "second": [4]}) == {"3", "4"}
    assert _ids("[13779, 13780]") == {"13779", "13780"}
    assert _ids("[not ids") == set() and _ids(None) == set()


def test_10m_plans_flatten_in_plan_then_batch_order():
    chat = [{"plan-2": None, "plan-1": [{"batch_number": 1, "time_anchor": None,
                                         "turns": [[_message(0, "start", "July-01-2024"), _message(1, "ok", role="assistant")]]}]},
            {"plan-1": None, "plan-2": [{"batch_number": 1, "time_anchor": None,
                                         "turns": [[_message(2, "later", "August-01-2024")]]}]}]
    sessions = _beam_sessions(chat)
    assert [name for name, _ in sessions] == ["plan-1 batch 1", "plan-2 batch 1"]
    assert [m["id"] for _, messages in sessions for m in messages] == [0, 1, 2]


def test_restarted_message_ids_become_unique_and_ambiguous_citations_are_dropped(tmp_path):
    chat = [[_message(0, "first session", "March-01-2024"), _message(1, "unique")],
            [_message(0, "id restarted in a later session", "April-01-2024")]]
    path = tmp_path / "beam1m.parquet"
    pd.DataFrame([{"conversation_id": 5, "chat": chat, "probing_questions": repr(_questions([0, 1]))}]).to_parquet(path)
    [(_space, sessions, _now, questions)] = list(beam_cases(str(path)))
    assert [m["id"] for _, _, messages in sessions for m in messages] == ["0@1", "1", "0@2"]
    assert {q["type"]: q["evidence"] for q in questions}["information_extraction"] == {"1"}


def test_10m_rows_load_with_dates_evidence_and_rubrics(tmp_path):
    chat = [{"plan-1": [{"batch_number": 1, "time_anchor": None,
                         "turns": [[_message(0, "Postgres listens on 5432 ->-> 1,1", "July-01-2024")]]}]}]
    path = tmp_path / "beam10m.parquet"
    pd.DataFrame([{"conversation_id": 7, "chat": chat, "probing_questions": repr(_questions("[0]"))}]).to_parquet(path)
    [(space, sessions, now, questions)] = list(beam_cases(str(path)))
    assert space == "beam7" and now is None
    [(name, date, messages)] = sessions
    assert (name, date) == ("plan-1 batch 1", "2024-07-01")
    assert messages[0]["text"] == "Postgres listens on 5432"
    by_type = {q["type"]: q for q in questions}
    assert by_type["information_extraction"]["evidence"] == {"0"}
    assert by_type["abstention"]["evidence"] == set()
    assert by_type["information_extraction"]["rubric"] == ["States port 5432"]
