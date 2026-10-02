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
        with pytest.raises(ValueError, match=field):
            outcomes.validate_outcome({field: True})

    @pytest.mark.parametrize("field", ["tokens_used", "llm_calls"])
    def test_a_numeric_field_rejects_a_float(self, field):
        with pytest.raises(ValueError, match=f"{field} must be an integer"):
            outcomes.validate_outcome({field: 1500.5})

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
    async def test_fleet_outcomes_reports_real_data_from_contribute_trace_alone(
        self, session_factory, org, config
    ):
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            for i in range(10):
                await crud.contribute_trace(
                    session, org, config, rate_limiter,
                    title=f"baseline {i}", context_text="c", solution_text="s",
                    tags=[], agent_type="code", actor="test",
                    outcome={"resolved": i < 5, "baseline": True},
                )
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
        assert resolution["baseline"]["n"] == 10
        assert resolution["current"]["n"] == 10
