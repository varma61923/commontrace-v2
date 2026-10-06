"""Malformed persistent settings stay safe across retrieval entry points."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from commontrace import gateway, retrieval, retrieval_io


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(retrieval_io, "default_fusion", lambda: retrieval_io.FUSION_NONE)
    monkeypatch.setattr(retrieval_io, "default_rerank", lambda: retrieval_io.RERANK_NONE)
    return str(tmp_path)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), -1.0])
def test_graph_weight_write_rejects_nonfinite_or_negative_without_writing(root, value):
    with pytest.raises(ValueError, match="graph weight must be finite"):
        retrieval_io.configure(root, graph_weight=value)
    assert not Path(retrieval_io.config_path(root)).exists()


@pytest.mark.parametrize(
    "raw, field, expected",
    [
        ({"floor": -0.1}, "floor", retrieval.DEFAULT_FLOOR),
        ({"floor": 1.1}, "floor", retrieval.DEFAULT_FLOOR),
        ({"floor": float("inf")}, "floor", retrieval.DEFAULT_FLOOR),
        ({"graph_weight": -0.1}, "graph_weight", 1.0),
        ({"graph_weight": float("nan")}, "graph_weight", 1.0),
        ({"graph_weight": float("inf")}, "graph_weight", 1.0),
        ({"max_lessons": float("inf")}, "max_lessons", retrieval_io.dosage.DEFAULT_MAX_LESSONS),
        ({"rrf_k": float("inf")}, "rrf_k", retrieval.DEFAULT_RRF_K),
    ],
)
def test_malformed_persisted_numbers_fall_back_per_field(root, raw, field, expected):
    target = Path(retrieval_io.config_path(root))
    target.parent.mkdir(parents=True)
    raw["max_chars"] = 321
    target.write_text(json.dumps(raw), encoding="utf-8")
    config = retrieval_io.load_config(root)
    assert getattr(config, field) == expected
    assert config.max_chars == 321, "one malformed field must not discard valid settings"


def test_valid_graph_weight_and_floor_roundtrip(root):
    config = retrieval_io.configure(root, floor=0.0, graph_weight=0.0)
    assert retrieval_io.load_config(root) == config
    config = retrieval_io.configure(root, floor=1.0, graph_weight=2.0)
    assert retrieval_io.load_config(root) == config


def test_numeric_fallback_handles_float_conversion_overflow():
    assert retrieval_io._float_or(10 ** 10000, 0.3) == 0.3


def test_gateway_unexpected_failure_logs_cause_but_keeps_response_private(root, caplog):
    app = gateway.Gateway(root, durable=False)

    def fail(_body, _query):
        raise RuntimeError("private store failure")

    app.routes[("GET", "/v1/health")] = (fail, {"auth": False})
    with caplog.at_level(logging.ERROR, logger="commontrace.gateway"):
        reply = app.handle("GET", "/v1/health")
    assert reply.status == 500
    assert json.loads(reply.body)["error"]["message"] == "RuntimeError"
    assert "private store failure" not in reply.body.decode()
    assert "RuntimeError" in caplog.text
    assert "private store failure" not in caplog.text
