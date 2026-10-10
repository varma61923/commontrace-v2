"""AMA-Bench loader: trajectory turns as messages, step-referenced turns as derived evidence."""
import json

from benchmarks import conversation_bench as cb


def _row(episode, domain="WEB"):
    return {"episode_id": episode, "task": "find the price", "task_type": "web", "domain": domain,
            "success": True, "num_turns": 3, "total_tokens": 10,
            "trajectory": [{"turn_idx": i, "action": f"click {i}", "observation": f"page {i}"} for i in range(4)],
            "qa_pairs": [
                {"question": "What did the agent click at Step 2?", "answer": "click 2", "question_uuid": "q1", "type": "A"},
                {"question": "Why did steps 1-3 repeat?", "answer": "a loop", "question_uuid": "q2", "type": "B"},
                {"question": "What was the overall goal?", "answer": "the price", "question_uuid": "q3", "type": "D"},
                {"question": "What happened at Step 99?", "answer": "nothing", "question_uuid": "q4", "type": "C"},
            ]}


def test_ama_cases_map_steps_to_action_and_observation_ids(tmp_path):
    path = tmp_path / "ama.jsonl"
    path.write_text("\n".join(json.dumps(_row(e)) for e in ("e1", "e2")))
    cases = list(cb.ama_cases(str(path)))
    assert len(cases) == 2
    space, sessions, _now, questions = cases[0]
    messages = sessions[0][2]
    assert messages[0]["text"].startswith("Task (WEB)") and messages[1]["text"] == "Step 0 action: click 0"
    by = {q["id"].rsplit("-", 1)[-1]: q for q in questions}
    assert by["q1"]["evidence"] == {f"{space}-s2-a", f"{space}-s2-o"} and by["q1"]["type"] == "recall"
    assert by["q2"]["evidence"] == {f"{space}-s{n}-{p}" for n in (1, 2, 3) for p in "ao"}
    assert by["q3"]["evidence"] == set()  # no step named: excluded from evidence metrics
    assert by["q4"]["evidence"] == set()  # a step outside the trajectory is not invented


def test_ama_limit_stratifies_by_domain(tmp_path):
    path = tmp_path / "ama.jsonl"
    path.write_text("\n".join(json.dumps(_row(f"e{i}", "WEB" if i < 5 else "GAME")) for i in range(10)))
    picked = list(cb.ama_cases(str(path), limit=4, seed=1))
    assert len(picked) == 4
    assert {c[1][0][2][0]["text"].split(")")[0] for c in picked} == {"Task (WEB", "Task (GAME"}


def test_ama_clips_megabyte_observations(tmp_path):
    row = _row("e1")
    row["trajectory"][1]["observation"] = "x" * (cb.AMA_FIELD_CHARS + 50)
    path = tmp_path / "ama.jsonl"
    path.write_text(json.dumps(row))
    [(_space, sessions, _now, _questions)] = list(cb.ama_cases(str(path)))
    clipped = [m["text"] for m in sessions[0][2] if m["id"].endswith("-s1-o")][0]
    assert clipped.endswith(" [truncated]") and len(clipped) < cb.AMA_FIELD_CHARS + 100
