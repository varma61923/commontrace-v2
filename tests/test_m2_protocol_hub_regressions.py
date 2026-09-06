"""Dedicated regression tests for Milestone M2: Hub, Commons, & Protocol Conformance.

Covers:
1. `validate_outcome` with explicit `None` for nullable outcome fields (resolved,
   escalated, repeated_error, frustration_signal, tokens_used, llm_calls).
2. `memory/lessons/lesson_template.md` schema conformance against lesson.schema.json.
3. `search_traces` SQL query inspection / mock confirming Trace.org_id == org_id is
   present in the retrieval counter update statement.
4. `amend_trace` wire dictionary retains `profile` during schema validation.
5. `protocol/schemas/trace.schema.json` declares and validates `shared_with_commons`,
   `quarantined`, and `quarantine_reason`.
6. `commons/eval/representations.py` Unicode token extraction support.
7. `hub/abuse.py` _SharedPgPool background thread termination on timeout.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest

pytest.importorskip("hub")

from commontrace import frontmatter, validate
from commons.eval import representations
from hub import abuse, outcomes


class TestValidateOutcomeNullableMetrics:
    """Tests for hub/outcomes.py:validate_outcome allowing None for nullable fields."""

    def test_validate_outcome_allows_all_nullable_metrics_as_none(self):
        payload = {
            "resolved": None,
            "escalated": None,
            "repeated_error": None,
            "frustration_signal": None,
            "tokens_used": None,
            "llm_calls": None,
        }
        res = outcomes.validate_outcome(payload)
        for key in payload:
            assert key in res
            assert res[key] is None

    def test_validate_outcome_preserves_valid_values_and_none(self):
        payload = {
            "resolved": True,
            "escalated": None,
            "repeated_error": False,
            "frustration_signal": None,
            "tokens_used": 1500,
            "llm_calls": None,
            "baseline": False,
        }
        res = outcomes.validate_outcome(payload)
        assert res["resolved"] is True
        assert res["escalated"] is None
        assert res["repeated_error"] is False
        assert res["frustration_signal"] is None
        assert res["tokens_used"] == 1500
        assert res["llm_calls"] is None
        assert res["baseline"] is False

    def test_validate_outcome_rejects_non_bool_for_proportions(self):
        for field in ("resolved", "escalated", "repeated_error", "frustration_signal"):
            with pytest.raises(ValueError, match=f"outcome\\.{field} must be a boolean"):
                outcomes.validate_outcome({field: "true"})
            with pytest.raises(ValueError, match=f"outcome\\.{field} must be a boolean"):
                outcomes.validate_outcome({field: 1})
            with pytest.raises(ValueError, match=f"outcome\\.{field} must be a boolean"):
                outcomes.validate_outcome({field: 0})

    def test_validate_outcome_rejects_non_int_for_means(self):
        for field in ("tokens_used", "llm_calls"):
            with pytest.raises(ValueError, match=f"outcome\\.{field} must be an integer"):
                outcomes.validate_outcome({field: "100"})
            with pytest.raises(ValueError, match=f"outcome\\.{field} must be an integer"):
                outcomes.validate_outcome({field: 100.5})
            with pytest.raises(ValueError, match=f"outcome\\.{field} must be an integer"):
                outcomes.validate_outcome({field: True})

    def test_validate_outcome_rejects_negative_means(self):
        for field in ("tokens_used", "llm_calls"):
            with pytest.raises(ValueError, match=f"outcome\\.{field} must not be negative"):
                outcomes.validate_outcome({field: -1})

    def test_tally_and_proportions_handle_none_values(self):
        trace_outcomes = [
            {"resolved": True, "tokens_used": 100},
            {"resolved": None, "tokens_used": None},
            {"resolved": False, "tokens_used": 300},
        ]
        t = outcomes.tally(trace_outcomes)
        # 1 success out of 2 evaluated (None excluded)
        assert t.props["resolved"] == (1, 2)
        # Mean 200 over 2 evaluated (None excluded)
        assert t.means["tokens_used"] == (200.0, 2)


class TestLessonTemplateSchemaConformance:
    """Tests for memory/lessons/lesson_template.md conforming to lesson.schema.json."""

    def test_lesson_template_validates_cleanly_against_schema(self):
        schema = validate.load_schema("lesson.schema.json")
        fm, _ = frontmatter.read("memory/lessons/lesson_template.md")
        errs = validate.validate(fm, schema)
        assert errs == [], f"Validation errors found on lesson_template.md: {errs}"

    def test_lesson_template_importance_rationale_min_length(self):
        fm, _ = frontmatter.read("memory/lessons/lesson_template.md")
        rationale = fm.get("importance_rationale")
        assert isinstance(rationale, str)
        assert len(rationale.strip()) >= 1


def _load_protocol_trace_schema() -> dict:
    path = REPO_ROOT / "protocol" / "schemas" / "trace.schema.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class TestTraceSchemaGovernanceAndCommonsFields:
    """Tests for protocol/schemas/trace.schema.json declaring governance and commons fields."""

    def test_trace_schema_accepts_shared_with_commons_and_quarantine_fields(self):
        schema = _load_protocol_trace_schema()
        trace_data = {
            "id": str(uuid.uuid4()),
            "title": "Fix memory leak in background worker",
            "context_text": "Worker thread failed to terminate after timeout.",
            "solution_text": "Signal stop event and cancel running tasks.",
            "tags": ["memory", "concurrency"],
            "agent_type": "code",
            "shared_with_commons": True,
            "quarantined": True,
            "quarantine_reason": "Suspicious payload pattern detected",
        }
        errs = validate.validate(trace_data, schema)
        assert errs == [], f"Expected clean validation, got: {errs}"

    def test_trace_schema_accepts_null_quarantine_reason(self):
        schema = _load_protocol_trace_schema()
        trace_data = {
            "id": str(uuid.uuid4()),
            "title": "Standard clean trace",
            "context_text": "Standard context",
            "solution_text": "Standard solution",
            "tags": ["standard"],
            "agent_type": "code",
            "shared_with_commons": False,
            "quarantined": False,
            "quarantine_reason": None,
        }
        errs = validate.validate(trace_data, schema)
        assert errs == [], f"Expected clean validation, got: {errs}"

    def test_trace_schema_backward_compatibility_without_optional_fields(self):
        schema = _load_protocol_trace_schema()
        trace_data = {
            "id": str(uuid.uuid4()),
            "title": "Minimal trace without governance fields",
            "context_text": "Context",
            "solution_text": "Solution",
            "tags": ["minimal"],
            "agent_type": "code",
        }
        errs = validate.validate(trace_data, schema)
        assert errs == [], f"Expected backward-compatible validation, got: {errs}"

    def test_trace_schema_rejects_invalid_types_for_governance_fields(self):
        schema = _load_protocol_trace_schema()
        base_trace = {
            "id": str(uuid.uuid4()),
            "title": "Trace with type violations",
            "context_text": "Context",
            "solution_text": "Solution",
            "tags": ["test"],
            "agent_type": "code",
        }

        # shared_with_commons must be boolean
        bad_shared = dict(base_trace, shared_with_commons="true")
        errs = validate.validate(bad_shared, schema)
        assert any("shared_with_commons" in e for e in errs)

        # quarantined must be boolean
        bad_quarantined = dict(base_trace, quarantined=1)
        errs = validate.validate(bad_quarantined, schema)
        assert any("quarantined" in e for e in errs)

        # quarantine_reason must be string or null
        bad_reason = dict(base_trace, quarantine_reason=12345)
        errs = validate.validate(bad_reason, schema)
        assert any("quarantine_reason" in e for e in errs)

    def test_hub_validate_trace_with_governance_fields(self):
        pytest.importorskip("jsonschema", reason="hub[server] extra not installed in this env")
        from hub.schema_validation import SchemaValidationError, validate_trace

        valid_trace = {
            "id": str(uuid.uuid4()),
            "title": "Valid trace for Hub validation",
            "context_text": "Context",
            "solution_text": "Solution",
            "tags": ["test"],
            "agent_type": "code",
            "shared_with_commons": True,
            "quarantined": False,
            "quarantine_reason": None,
        }
        # Should not raise
        validate_trace(valid_trace)

        bad_trace = dict(valid_trace, shared_with_commons="yes")
        with pytest.raises(SchemaValidationError):
            validate_trace(bad_trace)


class TestSearchTracesTenantIsolation:
    """Tests for hub/crud.py:search_traces retrieval counter update SQL query."""

    def test_search_traces_retrieval_update_includes_org_id_in_where_clause(self):
        pytest.importorskip("sqlalchemy", reason="hub[server] extra not installed in this env")

        async def _test():
            from sqlalchemy.sql.dml import Update
            from hub import crud
            from hub.models import Trace

            session = AsyncMock()
            org_id = str(uuid.uuid4())
            trace_id = str(uuid.uuid4())
            mock_trace = Trace(
                id=trace_id,
                org_id=org_id,
                title="Isolated Trace",
                context_text="Context",
                solution_text="Solution",
                tags=["isolation"],
                agent_type="code",
                quarantined=False,
            )

            scalars_mock = MagicMock()
            scalars_mock.all.return_value = [mock_trace]
            result_mock = MagicMock()
            result_mock.scalars.return_value = scalars_mock

            session.execute.return_value = result_mock

            with patch("hub.crud._hydrate", new_callable=AsyncMock) as mock_hydrate:
                mock_hydrate.return_value = []
                await crud.search_traces(session, org_id=org_id)

            update_calls = [
                call for call in session.execute.call_args_list
                if len(call[0]) > 0 and isinstance(call[0][0], Update)
            ]
            assert len(update_calls) == 1, "Expected exactly one UPDATE statement execution"
            update_stmt = update_calls[0][0][0]

            # Verify the where clause contains both org_id constraint and id.in_ constraint
            where_clauses = list(update_stmt.whereclause.clauses)
            where_strs = [str(clause) for clause in where_clauses]

            assert any("traces.org_id" in s for s in where_strs), (
                f"Trace.org_id constraint missing from WHERE clause: {where_strs}"
            )
            assert any("traces.id" in s for s in where_strs), (
                f"Trace.id constraint missing from WHERE clause: {where_strs}"
            )

        asyncio.run(_test())


class TestAmendTraceProfileRetention:
    """Tests for hub/crud.py:amend_trace preserving profile in wire validation."""

    def test_amend_trace_passes_profile_to_wire_validation(self):
        pytest.importorskip("sqlalchemy", reason="hub[server] extra not installed in this env")

        async def _test():
            from hub import crud
            from hub.abuse import make_rate_limiter
            from hub.config import HubConfig
            from hub.models import Trace
            from hub.plans import DEFAULT_PLAN, get

            session = AsyncMock()
            session.add = MagicMock()  # synchronous in SQLAlchemy
            org_id = str(uuid.uuid4())
            trace_id = str(uuid.uuid4())
            original_trace = Trace(
                id=trace_id,
                org_id=org_id,
                title="Original Trace Title",
                context_text="Original Context",
                solution_text="Original Solution",
                tags=["profile-test"],
                agent_type="code",
                profile="specialized-code-profile",
                quarantined=False,
                depth=0,
            )

            scalars_mock = MagicMock()
            scalars_mock.scalar_one_or_none.return_value = original_trace
            result_mock = MagicMock()
            result_mock.scalar_one_or_none = scalars_mock.scalar_one_or_none
            session.execute.return_value = result_mock

            config = HubConfig("postgresql+asyncpg://localhost/test")
            limiter = make_rate_limiter(config)

            with patch("hub.crud._plan_for", new_callable=AsyncMock) as mock_plan, \
                 patch("hub.crud.validate_trace") as mock_validate_trace, \
                 patch("hub.crud._hydrate_one", new_callable=AsyncMock) as mock_hydrate_one:
                mock_plan.return_value = get(DEFAULT_PLAN)
                mock_hydrate_one.return_value = {"id": str(uuid.uuid4())}

                await crud.amend_trace(
                    session,
                    org_id,
                    trace_id,
                    config,
                    limiter,
                    title="Amended Trace Title",
                )

                assert mock_validate_trace.called, "validate_trace should have been called"
                wire_dict = mock_validate_trace.call_args[0][0]
                assert "profile" in wire_dict, "profile key missing from wire dictionary"
                assert wire_dict["profile"] == "specialized-code-profile", (
                    f"Expected profile to be preserved as 'specialized-code-profile', got {wire_dict['profile']!r}"
                )

        asyncio.run(_test())


class TestCommonsEvalRepresentationsUnicode:
    """Tests for commons/eval/representations.py Unicode token extraction."""

    def test_word_regex_extracts_unicode_tokens(self):
        sample = "déjà vu résumé connexion connexión 日本語 123"
        tokens = representations._WORD.findall(sample)
        assert "déjà" in tokens or ("d" in tokens and "j" in tokens)
        assert "connexión" in tokens
        assert "日本語" in tokens

    def test_words_extracts_accented_and_cjk_tokens(self):
        text = "déjà vu connexión 日本語 python"
        result = representations.words(text)
        assert "connexión" in result
        assert "日本語" in result
        assert "python" in result


class TestSharedPgPoolThreadCleanupOnTimeout:
    """Tests for hub/abuse.py:_SharedPgPool clean termination on timeout."""

    def test_shared_pg_pool_cleans_up_thread_on_timeout(self):
        async def hanging_setup(*args, **kwargs):
            await asyncio.sleep(10)

        fake_asyncpg = MagicMock()
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            with patch.object(abuse, "_POOL_STARTUP_TIMEOUT_SECONDS", 0.05):
                with patch.object(abuse._SharedPgPool, "_setup", hanging_setup):
                    with pytest.raises(TimeoutError, match="timed out after 0.05s"):
                        abuse._SharedPgPool("postgresql://user:pass@localhost:5432/testdb")

        # Allow thread event loop to process cancellation and exit
        time.sleep(0.1)
        alive_threads = [t for t in threading.enumerate() if t.name == "hub-ratelimit-pg"]
        assert len(alive_threads) == 0, f"Thread leaked after timeout: {alive_threads}"
