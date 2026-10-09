"""Adaptive recall budgets: sized by question shape, opt-in, cache-safe and explained."""
import json

import pytest

from commontrace.cli import main
from commontrace.conversation import ConversationError, Options, Store, recall
from commontrace.conversation.search import budget_for


@pytest.mark.parametrize("question,reason,factor", [
    ("Where does Ana work now?", "focused", 1.0),
    ("What is the name of my dog?", "focused", 1.0),
    ("Can you summarize what we discussed about the migration?", "summary", 3.0),
    ("In what order did I bring up the three issues?", "ordering", 3.0),
    ("How many books have I read this year?", "counting", 3.0),
    ("What activities does Melanie partake in?", "list", 2.0),
    ("What kind of art does Caroline make?", "list", 2.0),
    ("Which city have both Jean and John visited?", "list", 2.0),
])
def test_budget_for_classifies_question_shapes(question, reason, factor):
    assert budget_for(question, 1000) == (int(1000 * factor), reason)


def test_budget_for_never_exceeds_the_cap_or_drops_below_base():
    assert budget_for("Summarize everything", 5000, cap=8000) == (8000, "summary")
    assert budget_for("Where do I live?", 5000, cap=8000)[0] == 5000
    with pytest.raises(ConversationError):
        budget_for("x", 1000, cap=500)
    with pytest.raises(ConversationError):
        budget_for("x", 0)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path), "ana")
    for day in range(1, 31):
        s.add(f"s{day}", [{"speaker": "Ana", "text": f"On day {day} I painted a landscape with color {day} "
                                                     + "and long notes " * 30}],
              session_at=f"2023-05-{day:02d}")
    yield s
    s.close()


def test_adaptive_recall_uses_more_context_for_a_list_question(store):
    question = "What paintings has Ana made?"
    fixed = recall(store, question, options=Options(budget=300, embedder=None, rerank=None))
    adaptive = recall(store, question, options=Options(budget=300, embedder=None, rerank=None, adaptive_budget=True))
    assert adaptive.explain["budget"] == {"requested": 300, "effective": 600, "reason": "list"}
    assert fixed.tokens <= 300 < adaptive.tokens <= 600
    assert "budget" not in fixed.explain
    # The cached repeat still reports its budget decision.
    again = recall(store, question, options=Options(budget=300, embedder=None, rerank=None, adaptive_budget=True))
    assert again.explain["budget"]["effective"] == 600 and again.context == adaptive.context


def test_focused_question_is_unchanged_by_adaptive_mode(store):
    question = "What color did Ana use on day 7?"
    fixed = recall(store, question, options=Options(budget=400, embedder=None, rerank=None))
    adaptive = recall(store, question, options=Options(budget=400, embedder=None, rerank=None, adaptive_budget=True))
    assert adaptive.explain["budget"]["reason"] == "focused"
    assert adaptive.context == fixed.context


def test_cli_budget_auto(tmp_path, capsys):
    root = str(tmp_path)
    msgs = tmp_path / "m.json"
    msgs.write_text(json.dumps([{"speaker": "Ana", "text": "I painted a landscape and a portrait."}]))
    assert main(["conversation", "add", "ana", "--file", str(msgs), "--session", "s1", "--dest", root]) == 0
    capsys.readouterr()
    assert main(["conversation", "recall", "ana", "What paintings has Ana made?", "--budget", "auto",
                 "--lexical", "--json", "--dest", root]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["explain"]["budget"] == {"requested": 1500, "effective": 3000, "reason": "list"}
    assert main(["conversation", "recall", "ana", "q", "--budget", "500", "--max-budget", "900",
                 "--lexical", "--dest", root]) == 2
