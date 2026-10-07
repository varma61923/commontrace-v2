"""A concurrent curator cannot replay revoked instructions from cached eligibility."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from commontrace import frontmatter, lesson_cache, mcp_server

pytest.importorskip("mcp")


@pytest.mark.parametrize("core", [False, True])
@pytest.mark.parametrize("change", [
    {"status": "review"}, {"status": "archived"}, {"scopes": ["other-project"]},
    {"valid_until": "2020-01-01"}, {"valid_from": "2999-01-01"},
    {"name": "replaced-identity"}, {"agent_type": "sales"},
])
def test_fresh_body_requires_current_approval_and_eligibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, core: bool, change: dict[str, Any],
) -> None:
    path = tmp_path / "memory" / "lessons" / "lesson_recovery.md"
    path.parent.mkdir(parents=True)
    cached: dict[str, Any] = {
        "name": "recovery", "status": "active", "agent_type": "support", "core": core,
        "description": "Recover postgres database transaction connection failures safely",
        "applies_when": "A postgres database transaction connection fails", "importance": 4,
        "scopes": ["payments"], "tags": ["postgres", "database", "transaction"],
    }
    frontmatter.write(str(path), {**cached, **change}, "A recently edited instruction must not be replayed.")
    # Reproduce the actual scan/read window: ranking owns earlier approved
    # metadata, while the authoritative file already carries the newer edit.
    monkeypatch.setattr(lesson_cache, "load_active_with_terms", lambda *args, **kwargs: ([(str(path), cached)], None))
    server = mcp_server.build_server(str(tmp_path))

    async def drive() -> dict[str, Any]:
        result = await server.call_tool("retrieve", {
            "task": "Recover postgres database transaction connection failures safely",
            "scope": "payments", "agent_type": "support",
        })
        return dict(json.loads(result.content[0].text))

    result = asyncio.run(drive())
    assert result["ok"] is True
    assert result["lessons"] == []
    assert "recently edited instruction" not in json.dumps(result)


def test_core_designation_change_cannot_bypass_the_context_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "memory" / "lessons" / "lesson_core.md"
    path.parent.mkdir(parents=True)
    cached = {"name": "core", "status": "active", "core": True, "importance": 4,
              "description": "postgres database transaction recovery", "tags": ["postgres"]}
    frontmatter.write(str(path), {**cached, "core": False}, "A formerly core instruction.")
    monkeypatch.setattr(lesson_cache, "load_active_with_terms", lambda *args, **kwargs: ([(str(path), cached)], None))
    server = mcp_server.build_server(str(tmp_path))
    result = asyncio.run(server.call_tool("retrieve", {"task": "postgres database transaction recovery"}))
    decoded = json.loads(result.content[0].text)
    assert decoded["ok"] is True and decoded["lessons"] == []
