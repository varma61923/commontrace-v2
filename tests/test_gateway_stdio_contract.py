"""Malformed clients cannot terminate the gateway's local JSON stream."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from commontrace.gateway import Gateway, serve_stdio


@pytest.mark.parametrize("message", [
    {"op": []},
    {"op": {}},
    {"op": None},
    {"method": [], "path": "/v1/health"},
    {"method": "GET", "path": []},
    {"method": "GET", "path": {}},
    {"method": "GET", "path": None},
    {"method": "POST", "path": "/v1/recall", "body": []},
    {"method": "POST", "path": "/v1/recall", "body": "text"},
])
def test_invalid_request_shape_preserves_following_requests(
    tmp_path: Path, message: dict[str, Any],
) -> None:
    """An error is one response, followed by successful ordinary traffic."""
    gateway = Gateway(str(tmp_path), token="test-only-token")
    requests = [{"id": "invalid", **message}, {"id": "next", "op": "health"}]
    stdin = io.StringIO("\n".join(json.dumps(row) for row in requests) + "\n")
    stdout = io.StringIO()

    assert serve_stdio(gateway, stdin, stdout) == 0

    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert len(replies) == 2
    assert replies[0]["id"] == "invalid"
    assert replies[0]["status"] == 400
    assert replies[0]["body"]["error"]["code"] == "bad_request"
    assert replies[1]["id"] == "next"
    assert replies[1]["status"] == 200
    assert replies[1]["body"]["ok"] is True


def test_invalid_json_preserves_following_requests(tmp_path: Path) -> None:
    gateway = Gateway(str(tmp_path), token="test-only-token")
    stdin = io.StringIO('{not-json}\n{"id": "next", "op": "health"}\n')
    stdout = io.StringIO()

    assert serve_stdio(gateway, stdin, stdout) == 0

    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [row["status"] for row in replies] == [400, 200]
    assert replies[1]["id"] == "next"


def test_excessively_nested_json_preserves_following_requests(tmp_path: Path) -> None:
    gateway = Gateway(str(tmp_path), token="test-only-token")
    nested = '{"body":' + '[' * 2000 + '0' + ']' * 2000 + '}'
    stdin = io.StringIO(nested + '\n{"id": "next", "op": "health"}\n')
    stdout = io.StringIO()

    assert serve_stdio(gateway, stdin, stdout) == 0

    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [row["status"] for row in replies] == [400, 200]
    assert replies[1]["id"] == "next"
