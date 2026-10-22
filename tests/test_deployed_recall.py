"""The deployed-gateway benchmark writes and asks over HTTP and checks parity with in-process recall."""
from __future__ import annotations

import json

import pytest

from benchmarks import deployed_recall

pytestmark = pytest.mark.e2e


def _locomo(path):
    conversation = {
        "speaker_a": "Ana", "speaker_b": "Ben",
        "session_1_date_time": "1:56 pm on 8 May, 2023",
        "session_1": [
            {"speaker": "Ana", "dia_id": "D1:1", "text": "I adopted a beagle named Pepper last week."},
            {"speaker": "Ben", "dia_id": "D1:2", "text": "Congratulations! Where did you find her?"},
            {"speaker": "Ana", "dia_id": "D1:3", "text": "At the shelter on Pine Street."},
        ],
        "session_2_date_time": "10:00 am on 20 June, 2023",
        "session_2": [
            {"speaker": "Ben", "dia_id": "D2:1", "text": "I started learning the cello in June."},
        ],
    }
    qa = [{"question": "What is the name of Ana's dog?", "answer": "Pepper", "evidence": ["D1:1"], "category": 4},
          {"question": "Which instrument is Ben learning?", "answer": "cello", "evidence": ["D2:1"], "category": 4}]
    path.write_text(json.dumps([{"sample_id": "conv-1", "conversation": conversation, "qa": qa}]))


def test_gateway_serves_the_same_contexts_as_in_process_recall(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("UNIMEM_CONVERSATION_EMBEDDER", "none")
    data = tmp_path / "locomo.json"
    _locomo(data)
    out = tmp_path / "result.json"
    code = deployed_recall.main(["--dataset", "locomo", "--data", str(data), "--root", str(tmp_path / "store"),
                                 "--budget", "200", "--adaptive-budget", "--max-budget", "400",
                                 "--concurrency", "2", "--out", str(out)])
    result = json.loads(out.read_text())
    assert code == 0
    assert result["parity"] == {"identical_contexts": 2, "of": 2}
    assert result["ingest"]["turns"] == 4 and result["questions"] == 2
    assert result["overall"]["evidence"] == 1.0
    assert result["recall"]["p50_ms"] > 0 and result["recall"]["queries_per_second"] > 0
