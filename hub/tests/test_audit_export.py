"""hub/audit_export.py and `hub.manage export-audit`."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from hub import audit, audit_export, manage
from hub.db import session_scope


def _entry(**overrides):
    base = dict(
        id="e1", created_at=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc), actor="operator-cli",
        org_id="o1", action="purge_trace", target_type="trace", target_id="t1", summary="removed one",
    )
    base.update(overrides)
    return NS(**base)


class TestFormats:
    def test_jsonl_round_trips_every_field(self):
        row = json.loads(audit_export.to_jsonl(_entry()))
        assert row == {
            "id": "e1", "time": "2026-10-01T12:00:00+00:00", "actor": "operator-cli", "org_id": "o1",
            "action": "purge_trace", "target_type": "trace", "target_id": "t1", "summary": "removed one",
        }

    def test_cef_header_and_extension(self):
        line = audit_export.to_cef(_entry())
        assert line.startswith("CEF:0|CommonTrace|Hub|")
        assert "|purge_trace|purge_trace|8|" in line
        assert "suser=operator-cli" in line and "cs1=o1" in line and "cs2=trace:t1" in line
        assert "rt=1790856000000" in line

    def test_cef_escapes_separators_and_newlines(self):
        line = audit_export.to_cef(_entry(action="a|b", summary="x=1\ny\\z"))
        assert "|a\\|b|" in line
        assert "msg=x\\=1\\ny\\\\z" in line
        assert "\n" not in line

    def test_severity_marks_destructive_actions(self):
        assert audit_export.severity("revoke_key") == 8
        assert audit_export.severity("create_org") == 3

    def test_unknown_format_is_refused(self):
        with pytest.raises(ValueError):
            audit_export.render(_entry(), "xml")


@pytest.mark.asyncio
class TestExportCommand:
    async def _seed(self, session_factory):
        async with session_scope(session_factory) as session:
            for i, org in enumerate(("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222")):
                await audit.record(session, actor="operator-cli", action=f"create_org_{i}", org_id=org)

    async def test_exports_oldest_first_one_line_each(self, session_factory, capsys):
        await self._seed(session_factory)
        assert await manage.export_audit("jsonl", session_factory=session_factory) is True
        lines = capsys.readouterr().out.strip().splitlines()
        assert [json.loads(line)["action"] for line in lines] == ["create_org_0", "create_org_1"]

    async def test_since_is_a_high_water_mark(self, session_factory, capsys):
        await self._seed(session_factory)
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        await manage.export_audit("jsonl", future, session_factory=session_factory)
        assert capsys.readouterr().out == ""

    async def test_org_filter(self, session_factory, capsys):
        await self._seed(session_factory)
        await manage.export_audit("cef", None, "22222222-2222-2222-2222-222222222222",
                                  session_factory=session_factory)
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 1 and "create_org_1" in lines[0]

    async def test_bad_format_is_an_operator_error(self, session_factory):
        with pytest.raises(ValueError):
            await manage.export_audit("xml", session_factory=session_factory)
