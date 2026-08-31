"""`Trace.outcome` must actually be settable, or `fleet_outcomes` has
nothing to measure.

Before this file's fix, `contribute_trace` had no `outcome` parameter at
all -- not merely unvalidated, genuinely absent from both the crud.py
function and the MCP tool signature -- despite hub/README.md and
hub/outcomes.py's own `_is_bool` docstring both stating flatly that
"contribute_trace writes all of it" / "contribute_trace accepts the
outcome object largely as given". `amend_trace` only ever carried the
original's outcome forward unchanged (`outcome=dict(original.outcome or
{})`), with no override parameter either. `hub/bench_scaling.py`'s raw-SQL
seed script and test fixtures that build a `Trace(...)` ORM object
directly were the ONLY things that ever produced a non-empty outcome dict
-- so for any real customer, calling any documented, MCP-exposed tool,
`fleet_outcomes` could only ever report "No baseline window recorded, so
there is nothing to compare against", forever, regardless of how their
fleet actually performed. `outcomes.py`'s statistics, `fleet_outcomes`'s
SQL aggregation, and this file's sibling `test_fleet_outcomes.py` were all
correct and all tested -- against an input path that did not exist.

This file tests the input side specifically: that `outcome` is now
actually accepted, validated the same way every other malformed-input case
in this Hub already is, merged (not replaced) on amend, correctly
idempotency-hashed, and -- the test that proves the loop is actually
closed -- that a `fleet_outcomes` report built entirely from
`contribute_trace` calls made through this module's own public API, with
no direct ORM construction anywhere, comes back with real data instead of
"nothing to compare against".
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from hub import crud, outcomes
from hub.abuse import make_rate_limiter
from hub.crud import IdempotencyKeyConflict
from hub.db import session_scope
from hub.models import Organization

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="outcome-input-org")
        session.add(o)
        await session.flush()
        return o.id


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
class TestValidateOutcomeItself:
    def test_none_becomes_empty_dict(self):
        assert outcomes.validate_outcome(None) == {}

    def test_empty_dict_passes_through(self):
        assert outcomes.validate_outcome({}) == {}

    def test_a_full_valid_outcome_passes_through_unchanged(self):
        full = {
            "resolved": True, "escalated": False, "repeated_error": False,
            "frustration_signal": False, "tokens_used": 800, "llm_calls": 3,
            "baseline": True,
        }
        assert outcomes.validate_outcome(full) == full

    def test_not_a_dict_raises(self):
        with pytest.raises(ValueError, match="object"):
            outcomes.validate_outcome("resolved")  # type: ignore[arg-type]

    def test_unknown_field_raises_naming_it(self):
        with pytest.raises(ValueError, match="success"):
            outcomes.validate_outcome({"success": True})

    @pytest.mark.parametrize("field", ["resolved", "escalated", "repeated_error", "frustration_signal", "baseline"])
    def test_a_boolean_field_rejects_a_non_bool(self, field):
        with pytest.raises(ValueError, match=field):
            outcomes.validate_outcome({field: "true"})

    @pytest.mark.parametrize("field", ["tokens_used", "llm_calls"])
    def test_a_numeric_field_rejects_a_non_number(self, field):
        with pytest.raises(ValueError, match=field):
            outcomes.validate_outcome({field: "800"})

    @pytest.mark.parametrize("field", ["tokens_used", "llm_calls"])
    def test_a_numeric_field_rejects_a_bool(self, field):
        """isinstance(True, int) is True in Python -- a resolved:true
        misfiled under tokens_used would otherwise be averaged in as 1."""
        with pytest.raises(ValueError, match=field):
            outcomes.validate_outcome({field: True})

    @pytest.mark.parametrize("field", ["tokens_used", "llm_calls"])
    def test_a_numeric_field_rejects_a_negative_value(self, field):
        with pytest.raises(ValueError, match=field):
            outcomes.validate_outcome({field: -1})

    def test_a_numeric_field_accepts_zero(self):
        assert outcomes.validate_outcome({"tokens_used": 0}) == {"tokens_used": 0}

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"])
    def test_is_number_itself_rejects_nan_and_infinity(self, bad):
        assert outcomes._is_number(bad) is False

    def test_is_number_itself_accepts_ordinary_finite_values(self):
        assert outcomes._is_number(0) is True
        assert outcomes._is_number(3.5) is True
        assert outcomes._is_number(-2) is True

    @pytest.mark.parametrize("field", ["tokens_used", "llm_calls"])
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"])
    def test_a_numeric_field_rejects_nan_and_infinity(self, field, bad):
        """`value < 0` is False for both NaN (any IEEE 754 comparison with
        NaN is False) and +Infinity, so the negative-value check alone
        cannot catch either -- reproduced against a live Postgres before
        this fix: a NaN reached a JSONB column and crashed uncaught with
        asyncpg.exceptions.InvalidTextRepresentationError (Postgres's json
        parser rejects the literal token `NaN`, which is what Python's
        json.dumps produces for a NaN float by default)."""
        with pytest.raises(ValueError, match=field):
            outcomes.validate_outcome({field: bad})


class TestContributeTraceOutcome:
    async def test_a_valid_outcome_is_stored(self, session_factory, org, config):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            result = await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="t", context_text="c", solution_text="s", tags=[],
                agent_type="code", actor="test",
                outcome={"resolved": True, "tokens_used": 500},
            )
        async with session_scope(session_factory) as session:
            fetched = await crud.get_trace(session, org, result["id"])
        assert fetched["outcome"] == {"resolved": True, "tokens_used": 500}

    async def test_omitted_outcome_stores_an_empty_dict(self, session_factory, org, config):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            result = await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="t", context_text="c", solution_text="s", tags=[],
                agent_type="code", actor="test",
            )
        async with session_scope(session_factory) as session:
            fetched = await crud.get_trace(session, org, result["id"])
        assert fetched["outcome"] == {}

    async def test_an_invalid_outcome_is_rejected_and_stores_nothing(self, session_factory, org, config):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="resolved"):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s", tags=[],
                    agent_type="code", actor="test",
                    outcome={"resolved": "yes"},
                )
        async with session_scope(session_factory) as session:
            result = await crud.search_traces(session, org, query="t")
        assert result["traces"] == []

    async def test_a_nan_token_count_is_rejected_rather_than_reaching_postgres(
        self, session_factory, org, config
    ):
        """Reproduced against a live Postgres before this fix: `nan < 0` is
        False (any IEEE 754 comparison with NaN is False), so the
        negative-value check alone let a NaN through, and it crashed
        uncaught at the INSERT with asyncpg's InvalidTextRepresentationError
        -- the same 500-instead-of-400 failure mode this whole module
        exists to prevent, in code added earlier in this same fix."""
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="tokens_used"):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s", tags=[],
                    agent_type="code", actor="test",
                    outcome={"tokens_used": float("nan")},
                )

    async def test_idempotent_retry_with_the_same_outcome_returns_the_original(
        self, session_factory, org, config
    ):
        rate_limiter = make_rate_limiter(config)
        key = "retry-key-1"
        outcome = {"resolved": True}
        async with session_scope(session_factory) as session:
            first = await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="t", context_text="c", solution_text="s", tags=[],
                agent_type="code", actor="test", idempotency_key=key, outcome=outcome,
            )
        async with session_scope(session_factory) as session:
            retry = await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="t", context_text="c", solution_text="s", tags=[],
                agent_type="code", actor="test", idempotency_key=key, outcome=outcome,
            )
        assert retry["id"] == first["id"]

    async def test_reusing_a_key_with_a_different_outcome_conflicts(self, session_factory, org, config):
        rate_limiter = make_rate_limiter(config)
        key = "retry-key-2"
        async with session_scope(session_factory) as session:
            await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="t", context_text="c", solution_text="s", tags=[],
                agent_type="code", actor="test", idempotency_key=key,
                outcome={"resolved": True},
            )
        async with session_scope(session_factory) as session:
            with pytest.raises(IdempotencyKeyConflict):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title="t", context_text="c", solution_text="s", tags=[],
                    agent_type="code", actor="test", idempotency_key=key,
                    outcome={"resolved": False},
                )


class TestAmendTraceOutcome:
    @pytest_asyncio.fixture
    async def trace_id(self, session_factory, org, config):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            t = await crud.contribute_trace(
                session, org, config, rate_limiter,
                title="ok", context_text="c", solution_text="s", tags=[],
                agent_type="code", actor="test",
            )
        return t["id"]

    async def test_amend_with_no_outcome_carries_the_original_forward(
        self, session_factory, org, config, trace_id
    ):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            first = await crud.amend_trace(
                session, org, trace_id, config, rate_limiter,
                outcome={"resolved": True}, actor="test",
            )
        # Amending the HEAD of the chain (first["id"]), not the original
        # trace_id again -- amend_trace never mutates anything in place, so
        # re-amending trace_id would supersede the still-unmutated original
        # (outcome still {}) rather than build on `first`.
        async with session_scope(session_factory) as session:
            result = await crud.amend_trace(
                session, org, first["id"], config, rate_limiter,
                title="retitled", actor="test",
            )
        assert result["outcome"] == {"resolved": True}

    async def test_amend_with_an_outcome_merges_rather_than_replaces(
        self, session_factory, org, config, trace_id
    ):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            first = await crud.amend_trace(
                session, org, trace_id, config, rate_limiter,
                outcome={"resolved": True}, actor="test",
            )
        async with session_scope(session_factory) as session:
            second = await crud.amend_trace(
                session, org, first["id"], config, rate_limiter,
                outcome={"tokens_used": 800}, actor="test",
            )
        assert second["outcome"] == {"resolved": True, "tokens_used": 800}

    async def test_amend_outcome_can_override_an_existing_key(
        self, session_factory, org, config, trace_id
    ):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            first = await crud.amend_trace(
                session, org, trace_id, config, rate_limiter,
                outcome={"resolved": False}, actor="test",
            )
        async with session_scope(session_factory) as session:
            second = await crud.amend_trace(
                session, org, first["id"], config, rate_limiter,
                outcome={"resolved": True}, actor="test",
            )
        assert second["outcome"] == {"resolved": True}

    async def test_an_invalid_outcome_is_rejected(self, session_factory, org, config, trace_id):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            with pytest.raises(ValueError, match="tokens_used"):
                await crud.amend_trace(
                    session, org, trace_id, config, rate_limiter,
                    outcome={"tokens_used": -5}, actor="test",
                )

    async def test_idempotent_retry_with_the_same_outcome_returns_the_original(
        self, session_factory, org, config, trace_id
    ):
        rate_limiter = make_rate_limiter(config)
        key = "amend-retry-key"
        async with session_scope(session_factory) as session:
            first = await crud.amend_trace(
                session, org, trace_id, config, rate_limiter,
                outcome={"resolved": True}, actor="test", idempotency_key=key,
            )
        async with session_scope(session_factory) as session:
            retry = await crud.amend_trace(
                session, org, trace_id, config, rate_limiter,
                outcome={"resolved": True}, actor="test", idempotency_key=key,
            )
        assert retry["id"] == first["id"]

    async def test_reusing_a_key_with_a_different_outcome_conflicts(
        self, session_factory, org, config, trace_id
    ):
        rate_limiter = make_rate_limiter(config)
        key = "amend-retry-key-2"
        async with session_scope(session_factory) as session:
            await crud.amend_trace(
                session, org, trace_id, config, rate_limiter,
                outcome={"resolved": True}, actor="test", idempotency_key=key,
            )
        async with session_scope(session_factory) as session:
            with pytest.raises(IdempotencyKeyConflict):
                await crud.amend_trace(
                    session, org, trace_id, config, rate_limiter,
                    outcome={"resolved": False}, actor="test", idempotency_key=key,
                )


class TestFleetOutcomesEndToEnd:
    """The test that proves the loop is actually closed: build every trace
    through contribute_trace's public API (no direct ORM construction, the
    way hub/bench_scaling.py's seed script and every other fixture in this
    test suite do it), and confirm fleet_outcomes reports real data instead
    of 'nothing to compare against'."""

    async def test_fleet_outcomes_reports_real_data_from_contribute_trace_alone(
        self, session_factory, org, config
    ):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            # A baseline window: 10 traces, 5 resolved.
            for i in range(10):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title=f"baseline {i}", context_text="c", solution_text="s",
                    tags=[], agent_type="code", actor="test",
                    outcome={"resolved": i < 5, "baseline": True},
                )
            # The current window: 10 traces, 9 resolved -- a real improvement.
            for i in range(10):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title=f"current {i}", context_text="c", solution_text="s",
                    tags=[], agent_type="code", actor="test",
                    outcome={"resolved": i < 9},
                )
        async with session_scope(session_factory) as session:
            report = await crud.fleet_outcomes(session, org)
        assert report["n_baseline_traces"] == 10
        assert report["n_current_traces"] == 10
        resolution = next(m for m in report["metrics"] if m["field"] == "resolved")
        assert resolution["baseline"]["rate"] == pytest.approx(0.5)
        assert resolution["current"]["rate"] == pytest.approx(0.9)
        # This is the assertion that would have failed against the old
        # code: before the fix, EVERY trace's outcome was {} regardless of
        # what was passed (there was nowhere to pass it), so baseline/
        # current rates would both be None and n_baseline_traces would
        # still show 10 traces existing while carrying no usable data.
        assert resolution["baseline"]["n"] == 10
        assert resolution["current"]["n"] == 10
