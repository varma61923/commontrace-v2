"""Tests for hub/manage.py's monitoring/admin commands: stats, the
quarantine review queue, and permanent deletion (purge-trace/purge-org --
the deletion path DATA_RETENTION.md previously documented as
unimplemented)."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from hub import auth, crud, manage, rbac
from hub.abuse import make_rate_limiter
from hub.crud import amend_trace, contribute_trace
from hub.db import session_scope
from hub.models import Organization, Trace, TraceRelation, User

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def two_orgs(session_factory):
    async with session_scope(session_factory) as session:
        org_a = Organization(name="org-a")
        org_b = Organization(name="org-b")
        session.add_all([org_a, org_b])
        await session.flush()
        return {"org_a": org_a.id, "org_b": org_b.id}


class TestCreateOrgWarnsOnDuplicateName:
    """Organization.name carries no DB uniqueness constraint, and every
    hub/manage.py operation resolves an org by org_id, never by name -- so
    a duplicate name cannot make an operation resolve the wrong org
    programmatically. The real risk is an operator scanning a listing by
    eye and picking the wrong row when two orgs share a display name.
    create_org warns (not blocks) when that happens."""

    async def test_first_org_with_a_name_is_silent(self, session_factory, capsys):
        await manage.create_org("Acme Corp", session_factory=session_factory)
        err = capsys.readouterr().err
        assert "WARN" not in err

    async def test_a_second_org_with_the_same_name_warns(self, session_factory, capsys):
        await manage.create_org("Acme Corp", session_factory=session_factory)
        capsys.readouterr()
        await manage.create_org("Acme Corp", session_factory=session_factory)
        err = capsys.readouterr().err
        assert "WARN" in err
        assert "Acme Corp" in err

    async def test_creation_still_succeeds_despite_the_warning(self, session_factory):
        await manage.create_org("Acme Corp", session_factory=session_factory)
        await manage.create_org("Acme Corp", session_factory=session_factory)
        async with session_scope(session_factory) as session:
            count = await session.scalar(
                select(func.count()).select_from(Organization).where(Organization.name == "Acme Corp")
            )
        assert count == 2


async def test_list_orgs_attributes_active_key_counts_correctly(session_factory, config, two_orgs, capsys):
    """Regression test for switching from one ApiKey query per org (in the
    loop) to a single grouped query looked up by org_id -- pins that a
    batched count doesn't get mixed up between orgs, which is the real risk
    a refactor like this introduces. org_a gets 2 active keys and 1 revoked
    (which must not count), org_b gets 1; a bug that summed instead of
    grouped, or grouped by the wrong key, would show up as wrong per-org
    numbers here even though the total across both is right either way."""
    async with session_scope(session_factory) as session:
        await auth.issue_api_key(session, two_orgs["org_a"], expires_days=90)
        await auth.issue_api_key(session, two_orgs["org_a"], expires_days=90)
        revoked = await auth.issue_api_key(session, two_orgs["org_a"], expires_days=90)
        await auth.revoke_api_key(session, revoked.key_id)
        await auth.issue_api_key(session, two_orgs["org_b"], expires_days=90)

    capsys.readouterr()
    await manage.list_orgs(session_factory=session_factory)
    out = capsys.readouterr().out
    for line in out.splitlines():
        if two_orgs["org_a"] in line:
            assert "active_keys=2" in line, line
        elif two_orgs["org_b"] in line:
            assert "active_keys=1" in line, line


async def test_stats_reports_zero_on_empty_db(session_factory, capsys):
    await manage.stats(session_factory=session_factory)
    out = capsys.readouterr().out
    assert "organizations:      0" in out
    assert "mean trust:          n/a" in out


async def test_stats_computes_real_counts_and_mean_trust(session_factory, config, two_orgs, capsys):
    """Regression test for switching from Python len()/fmean() over fully
    loaded ORM objects to SQL-side COUNT()/AVG() -- pins that the actual
    numbers still come out right, not just that the query doesn't crash."""
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        t1 = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="t1", context_text="c", solution_text="s", tags=[], agent_type="code", actor="test",
        )
        t2 = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="t2", context_text="c", solution_text="s", tags=[], agent_type="code", actor="test",
        )
    async with session_scope(session_factory) as session:
        await crud.vote_trace(session, two_orgs["org_a"], t1["id"], "up", actor="test")
        await crud.vote_trace(session, two_orgs["org_a"], t2["id"], "down", actor="test")

    capsys.readouterr()
    await manage.stats(session_factory=session_factory)
    out = capsys.readouterr().out
    assert "organizations:      2" in out
    assert "traces (total):     2" in out
    assert "votes:               2" in out
    # trust=1.0 and trust=0.0 -> mean 0.5
    assert "mean trust:          0.500" in out


async def test_list_quarantined_reports_none_cleanly(session_factory, capsys, two_orgs):
    await manage.list_quarantined(session_factory=session_factory)
    assert "no quarantined traces" in capsys.readouterr().out


async def test_release_quarantine_clears_flag(session_factory, config, two_orgs):
    rate_limiter = make_rate_limiter(config)
    spam_urls = " ".join(f"http://spam{i}.example.com" for i in range(config.suspect_url_threshold + 1))
    async with session_scope(session_factory) as session:
        result = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="spammy", context_text=spam_urls, solution_text="s", tags=[], agent_type="code",
        )
    assert result["quarantined"] is True

    await manage.release_quarantine(result["id"], session_factory=session_factory)

    async with session_scope(session_factory) as session:
        trace = await session.get(Trace, result["id"])
    assert trace.quarantined is False
    assert trace.quarantine_reason == ""


async def test_list_quarantined_shows_pending_review(session_factory, config, two_orgs, capsys):
    rate_limiter = make_rate_limiter(config)
    spam_urls = " ".join(f"http://spam{i}.example.com" for i in range(config.suspect_url_threshold + 1))
    async with session_scope(session_factory) as session:
        result = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="spammy title", context_text=spam_urls, solution_text="s", tags=[], agent_type="code",
        )

    capsys.readouterr()
    await manage.list_quarantined(session_factory=session_factory)
    out = capsys.readouterr().out
    assert result["id"] in out
    assert "spammy title" in out

    # filtering to the *other* org must not show org_a's quarantined trace
    capsys.readouterr()
    await manage.list_quarantined(two_orgs["org_b"], session_factory=session_factory)
    assert "no quarantined traces" in capsys.readouterr().out


async def test_release_quarantine_unknown_id_reports_error(session_factory, capsys):
    result = await manage.release_quarantine(
        "00000000-0000-0000-0000-000000000000", session_factory=session_factory
    )
    assert "no such trace" in capsys.readouterr().err
    # False (not just the stderr message) is what makes `main()` exit
    # non-zero for a failed destructive op -- see test_main_command_exit_codes.
    assert result is False


async def test_purge_trace_deletes_it_permanently(session_factory, config, two_orgs):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        result = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="t", context_text="c", solution_text="s", tags=[], agent_type="code",
        )

    await manage.purge_trace(result["id"], session_factory=session_factory)

    async with session_scope(session_factory) as session:
        trace = await session.get(Trace, result["id"])
    assert trace is None


async def test_purge_trace_cleans_dangling_relation_referencing_it(session_factory, config, two_orgs):
    """A relation row's related_trace_id is a plain column, not an FK -- it
    would otherwise survive the referenced trace being purged. purge_trace
    must clean that up explicitly (see its own docstring)."""
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        original = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="original", context_text="c", solution_text="s", tags=[], agent_type="code",
        )
        amended = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="amended", context_text="c", solution_text="s", tags=[], agent_type="code",
        )
        # Simulate what amend_trace's relation bookkeeping produces: a
        # SUPERSEDED_BY edge on `original` pointing at `amended`.
        session.add(
            TraceRelation(
                trace_id=original["id"], related_trace_id=amended["id"], relationship_type="SUPERSEDED_BY"
            )
        )

    await manage.purge_trace(amended["id"], session_factory=session_factory)

    async with session_scope(session_factory) as session:
        dangling = (
            await session.execute(select(TraceRelation).where(TraceRelation.related_trace_id == amended["id"]))
        ).scalars().all()
    assert dangling == []


async def test_purge_trace_on_an_amended_original_also_removes_the_amendment(
    session_factory, config, two_orgs
):
    """Regression test: purge_trace used to delete only the exact id it was
    given. amend_trace carries most content forward into a NEW row rather
    than mutating in place, so purging the ORIGINAL id left the amended
    row -- holding the same (or superset) content -- fully intact. A
    deletion request against one link in a chain must remove the whole
    logical trace, not just that link."""
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        original = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="MARKER-ORIGINAL", context_text="c", solution_text="s", tags=[], agent_type="code",
        )
    async with session_scope(session_factory) as session:
        amended = await amend_trace(
            session, two_orgs["org_a"], original["id"], config, rate_limiter,
            title="MARKER-AMENDED", actor="test",
        )

    await manage.purge_trace(original["id"], session_factory=session_factory)

    async with session_scope(session_factory) as session:
        original_row = await session.get(Trace, original["id"])
        amended_row = await session.get(Trace, amended["id"])
    assert original_row is None
    assert amended_row is None, "amending, then purging the ORIGINAL id, must also remove the amendment"


async def test_purge_trace_on_the_amendment_also_removes_the_original(session_factory, config, two_orgs):
    """Same chain, purged from the other end: deleting the newest version
    must also remove the older version it superseded."""
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        original = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="MARKER-ORIGINAL-2", context_text="c", solution_text="s", tags=[], agent_type="code",
        )
    async with session_scope(session_factory) as session:
        amended = await amend_trace(
            session, two_orgs["org_a"], original["id"], config, rate_limiter,
            title="MARKER-AMENDED-2", actor="test",
        )

    await manage.purge_trace(amended["id"], session_factory=session_factory)

    async with session_scope(session_factory) as session:
        original_row = await session.get(Trace, original["id"])
        amended_row = await session.get(Trace, amended["id"])
    assert amended_row is None
    assert original_row is None, "purging the newest version in a chain must also remove the original"


async def test_purge_trace_unrelated_traces_survive(session_factory, config, two_orgs):
    """The chain walk must not over-reach: an unrelated trace (never
    amended, no supersedes link) must survive purging a completely
    different trace."""
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        target = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="target", context_text="c", solution_text="s", tags=[], agent_type="code",
        )
        bystander = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="bystander", context_text="c", solution_text="s", tags=[], agent_type="code",
        )

    await manage.purge_trace(target["id"], session_factory=session_factory)

    async with session_scope(session_factory) as session:
        bystander_row = await session.get(Trace, bystander["id"])
    assert bystander_row is not None


async def test_amendment_chain_is_a_bounded_number_of_round_trips(session_factory, config, two_orgs):
    """The BFS-per-level implementation this replaced issued one query per
    LINK in the chain -- delete_trace is a customer-reachable MCP tool
    (unlike purge_trace, which is operator-only), and nothing caps how deep
    a chain gets: repeated `amend_trace` calls on the same trace is this
    codebase's own documented curation pattern ("each attaching whatever
    became known since"). A self-service delete on a chain built that way
    would otherwise hold a pooled connection open for one round trip per
    amendment. The recursive-CTE replacement must cost the same small,
    constant number of round trips regardless of chain depth -- and must
    still return exactly the right set of ids."""
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        trace = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="chain-0", context_text="c", solution_text="s", tags=[], agent_type="code",
        )
    chain_ids = {trace["id"]}
    current = trace
    depth = 30
    for i in range(depth):
        async with session_scope(session_factory) as session:
            current = await amend_trace(
                session, two_orgs["org_a"], current["id"], config, rate_limiter,
                title=f"chain-{i + 1}", actor="test",
            )
        chain_ids.add(current["id"])
    assert len(chain_ids) == depth + 1

    async with session_scope(session_factory) as session:
        calls = 0
        real_execute = session.execute

        async def counting_execute(*args, **kwargs):
            nonlocal calls
            calls += 1
            return await real_execute(*args, **kwargs)

        session.execute = counting_execute
        result = await crud.amendment_chain(session, trace["id"])

    assert result == chain_ids
    # One recursive CTE per link direction -- independent of `depth`, which
    # is the property this fix exists for. The BFS it replaced would have
    # issued one call per level, i.e. up to `depth` of them.
    assert calls <= 2, f"amendment_chain issued {calls} queries for a {depth}-deep chain"


async def test_amendment_chain_includes_a_fork_off_an_ancestor(session_factory, config, two_orgs):
    """amend_trace's own docstring documents a real, reachable way the
    supersession graph forks: a retried amend_trace call with no (or a
    different) idempotency_key against the same still-unmutated original
    creates a SECOND trace superseding it, rather than extending the chain.
    amendment_chain must still return the WHOLE connected component in that
    case -- delete_trace/purge_trace trust this set to be the trace's
    complete lineage, and a fork that silently falls outside it survives an
    operation documented (and audited) as deleting all of it.

    Shape: A -- B -- D (the "main" line amended twice), plus C, a second,
    independent amendment of B (the fork). Querying from D (an amendment
    of the fork point's own child, not of the fork point itself) must still
    reach C: C shares an ancestor with D, not a direct edge to it.
    """
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        a = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="a", context_text="c", solution_text="s", tags=[], agent_type="code",
        )
    async with session_scope(session_factory) as session:
        b = await amend_trace(
            session, two_orgs["org_a"], a["id"], config, rate_limiter, title="b", actor="test",
        )
    async with session_scope(session_factory) as session:
        d = await amend_trace(
            session, two_orgs["org_a"], b["id"], config, rate_limiter, title="d", actor="test",
        )
    async with session_scope(session_factory) as session:
        # A second, independent amendment of B -- the fork. No idempotency_key,
        # same as the retry scenario amend_trace's docstring describes.
        c = await amend_trace(
            session, two_orgs["org_a"], b["id"], config, rate_limiter, title="c", actor="test",
        )

    expected = {a["id"], b["id"], c["id"], d["id"]}
    async with session_scope(session_factory) as session:
        result_from_d = await crud.amendment_chain(session, d["id"])
        result_from_c = await crud.amendment_chain(session, c["id"])
    assert result_from_d == expected
    assert result_from_c == expected


async def test_purge_trace_unknown_id_reports_error(session_factory, capsys):
    result = await manage.purge_trace("00000000-0000-0000-0000-000000000000", session_factory=session_factory)
    assert "no such trace" in capsys.readouterr().err
    assert result is False


async def test_purge_org_cascades_to_its_traces(session_factory, config, two_orgs):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        result = await contribute_trace(
            session, two_orgs["org_a"], config, rate_limiter,
            title="t", context_text="c", solution_text="s", tags=[], agent_type="code",
        )

    await manage.purge_org(two_orgs["org_a"], session_factory=session_factory)

    async with session_scope(session_factory) as session:
        org = await session.get(Organization, two_orgs["org_a"])
        trace = await session.get(Trace, result["id"])
        other_org_still_there = await session.get(Organization, two_orgs["org_b"])
    assert org is None
    assert trace is None
    assert other_org_still_there is not None


async def test_purge_org_unknown_id_reports_error(session_factory, capsys):
    result = await manage.purge_org("00000000-0000-0000-0000-000000000000", session_factory=session_factory)
    assert "no such organization" in capsys.readouterr().err
    assert result is False


async def test_purge_org_cancels_a_live_stripe_subscription_first(
    session_factory, two_orgs, monkeypatch
):
    """An org row deleted out from under an active Stripe subscription
    keeps charging that customer's card every billing cycle with no
    CommonTrace account left to ever notice -- see
    billing.cancel_subscription's own docstring."""
    async with session_scope(session_factory) as session:
        org = await session.get(Organization, two_orgs["org_a"])
        org.stripe_customer_id = "cus_1"
        org.stripe_subscription_id = "sub_1"

    cancelled = {}

    async def fake_cancel(settings, *, subscription_id):
        cancelled["subscription_id"] = subscription_id

    from hub.billing import StripeSettings

    monkeypatch.setattr(manage, "cancel_subscription", fake_cancel)
    result = await manage.purge_org(
        two_orgs["org_a"], session_factory=session_factory, stripe=StripeSettings(secret_key="sk_test")
    )
    assert result is True
    assert cancelled["subscription_id"] == "sub_1"
    async with session_scope(session_factory) as session:
        assert await session.get(Organization, two_orgs["org_a"]) is None


async def test_purge_org_a_failed_cancellation_blocks_deletion(
    session_factory, two_orgs, monkeypatch, capsys
):
    """The org must survive intact so the operator can retry once
    whatever is stopping Stripe from being reachable clears."""
    async with session_scope(session_factory) as session:
        org = await session.get(Organization, two_orgs["org_a"])
        org.stripe_customer_id = "cus_1"
        org.stripe_subscription_id = "sub_1"

    from hub.billing import StripeError, StripeSettings

    async def failing_cancel(settings, *, subscription_id):
        raise StripeError("Stripe 500")

    monkeypatch.setattr(manage, "cancel_subscription", failing_cancel)
    result = await manage.purge_org(
        two_orgs["org_a"], session_factory=session_factory, stripe=StripeSettings(secret_key="sk_test")
    )
    assert result is False
    assert "could not cancel" in capsys.readouterr().err
    async with session_scope(session_factory) as session:
        surviving = await session.get(Organization, two_orgs["org_a"])
        assert surviving is not None
        assert surviving.stripe_subscription_id == "sub_1"


async def test_purge_org_with_no_subscription_never_calls_stripe(
    session_factory, two_orgs, monkeypatch
):
    async def must_not_be_called(*a, **kw):
        raise AssertionError("cancel_subscription must not run when there is nothing to cancel")

    monkeypatch.setattr(manage, "cancel_subscription", must_not_be_called)
    result = await manage.purge_org(two_orgs["org_a"], session_factory=session_factory)
    assert result is True


async def _submit_via_cli_path(session_factory, config, org_id, title="t"):
    rate_limiter = make_rate_limiter(config)
    async with session_scope(session_factory) as session:
        return await crud.submit_kb_entry(
            session, org_id, config, rate_limiter,
            title=title, context_text="c", solution_text="s", rationale="r", actor="test",
        )


class TestSubmissionReviewCommands:
    """hub/manage.py's operator wrappers around crud.review_kb_submission --
    the trust-tier-gated surface a community submission actually goes
    through to become Knowledge Base content."""

    async def test_list_submissions_reports_none_cleanly(self, session_factory, capsys):
        await manage.list_submissions(session_factory=session_factory)
        assert "no submissions" in capsys.readouterr().out

    async def test_list_submissions_shows_a_pending_one(self, session_factory, config, two_orgs, capsys):
        await _submit_via_cli_path(session_factory, config, two_orgs["org_a"], title="Stripe retries")
        await manage.list_submissions(session_factory=session_factory)
        out = capsys.readouterr().out
        assert "status=pending" in out
        assert "'Stripe retries'" in out

    async def test_list_submissions_rejects_a_bad_status_filter(self, session_factory, capsys):
        result = await manage.list_submissions("bogus", session_factory=session_factory)
        assert "must be one of" in capsys.readouterr().err
        assert result is False

    async def test_list_submissions_with_a_valid_status_filter(self, session_factory, config, two_orgs, capsys):
        """A valid status ("pending", not the default None) exercises
        crud.py:list_kb_submissions's own WHERE-clause filter, distinct
        from the unfiltered full-history listing the test above covers."""
        await _submit_via_cli_path(session_factory, config, two_orgs["org_a"], title="Stripe retries")
        await manage.list_submissions("pending", session_factory=session_factory)
        out = capsys.readouterr().out
        assert "status=pending" in out
        assert "'Stripe retries'" in out

    async def test_approve_submission_publishes_and_credits(self, session_factory, config, two_orgs, capsys):
        s = await _submit_via_cli_path(session_factory, config, two_orgs["org_a"], title="Stripe retries")
        result = await manage.approve_submission(s["id"], two_orgs["org_b"], session_factory=session_factory)
        assert result is True
        out = capsys.readouterr().out
        assert "approved" in out
        assert "new Knowledge Base entry" in out

        async with session_scope(session_factory) as session:
            org = await session.get(Organization, two_orgs["org_a"])
        assert org.bonus_commons_queries == crud.plans.SUBMISSION_ACCEPTANCE_CREDIT

    async def test_approve_submission_with_an_explicit_credit(self, session_factory, config, two_orgs):
        s = await _submit_via_cli_path(session_factory, config, two_orgs["org_a"])
        await manage.approve_submission(s["id"], two_orgs["org_b"], "42", session_factory=session_factory)
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, two_orgs["org_a"])
        assert org.bonus_commons_queries == 42

    async def test_approve_submission_reports_the_clamped_credit_not_the_raw_input(
        self, session_factory, config, two_orgs, capsys
    ):
        """review_kb_submission clamps `credit` to [0, 2**63-1] before
        writing it -- the operator-facing message must describe what was
        actually written to the database, not the raw --credit argument,
        or a negative (or absurdly large) value reads as granted when it
        was silently bounded to something else."""
        s = await _submit_via_cli_path(session_factory, config, two_orgs["org_a"])
        await manage.approve_submission(s["id"], two_orgs["org_b"], "-50", session_factory=session_factory)
        out = capsys.readouterr().out
        assert "credited 0 bonus" in out
        # Not a bare "-50" not in out: the org ids under test are random
        # UUIDs, and a UUID coincidentally containing the substring "-50"
        # (e.g. "...cb-50c2...") would fail this assertion for a reason
        # that has nothing to do with the raw credit leaking. Anchor to the
        # exact phrase the raw value would appear in if it leaked.
        assert "credited -50" not in out
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, two_orgs["org_a"])
        assert org.bonus_commons_queries == 0

    async def test_approve_submission_unknown_operator_org_reports_error(
        self, session_factory, config, two_orgs, capsys
    ):
        s = await _submit_via_cli_path(session_factory, config, two_orgs["org_a"])
        result = await manage.approve_submission(
            s["id"], "00000000-0000-0000-0000-000000000000", session_factory=session_factory,
        )
        assert "no such organization" in capsys.readouterr().err
        assert result is False

    async def test_approve_submission_unknown_submission_id_reports_error(
        self, session_factory, two_orgs, capsys
    ):
        result = await manage.approve_submission(
            "00000000-0000-0000-0000-000000000000", two_orgs["org_a"], session_factory=session_factory,
        )
        assert "no PENDING submission" in capsys.readouterr().err
        assert result is False

    async def test_reject_submission_records_reason_and_awards_nothing(
        self, session_factory, config, two_orgs, capsys
    ):
        s = await _submit_via_cli_path(session_factory, config, two_orgs["org_a"])
        result = await manage.reject_submission(s["id"], "too generic", session_factory=session_factory)
        assert result is True
        assert "rejected" in capsys.readouterr().out

        async with session_scope(session_factory) as session:
            org = await session.get(Organization, two_orgs["org_a"])
        assert org.bonus_commons_queries == 0

    async def test_reject_submission_unknown_id_reports_error(self, session_factory, capsys):
        result = await manage.reject_submission(
            "00000000-0000-0000-0000-000000000000", session_factory=session_factory,
        )
        assert "no PENDING submission" in capsys.readouterr().err
        assert result is False


@pytest.mark.filterwarnings("ignore:.*is marked with '@pytest.mark.asyncio'.*:pytest.PytestWarning")
class TestPurgeRequiresConfirmation:
    """purge-trace/purge-org are irreversible (no soft-delete, no undo).
    Without a confirmation gate, a mistyped id or an extra stray Enter in a
    terminal session silently deletes a customer's data with no chance to
    reconsider. `main()` now requires either --yes or an interactive 'yes'
    response before calling through to purge_trace/purge_org; direct
    Python calls to those functions (every other test in this file) are
    unaffected -- the gate lives in the CLI dispatch layer, not the
    function itself."""

    def test_refuses_without_yes_when_stdin_is_not_a_tty(self, config, monkeypatch, capsys):
        """pytest's captured stdin is never a tty, so this exercises the
        same non-interactive path a cron job or CI script would hit."""
        monkeypatch.setenv("HUB_DATABASE_URL", config.database_url)
        exit_code = manage.main(["purge-org", "00000000-0000-0000-0000-000000000000"])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert err.startswith("error: refusing")
        assert "--yes" in err

    def test_nothing_is_deleted_when_confirmation_is_refused(self, config, monkeypatch, capsys):
        # Every step goes through manage.main(), which builds and tears
        # down its own fresh engine/event loop per call (asyncio.run()
        # inside main()) -- mixing that with the pytest-asyncio
        # session_factory fixture's own loop caused asyncpg connections
        # bound to one loop to be used from another ("Task ... attached to
        # a different loop"). Chaining plain main() calls, the same
        # pattern test_malformed_uuid_reports_a_clean_error_not_a_traceback
        # already relies on, avoids that entirely.
        monkeypatch.setenv("HUB_DATABASE_URL", config.database_url)

        assert manage.main(["create-org", "confirm-gate-org"]) == 0
        org_id = capsys.readouterr().out.strip().removeprefix("org_id:").strip()

        exit_code = manage.main(["purge-org", org_id])
        assert exit_code == 2
        capsys.readouterr()

        # Org must still be listable -- purge_org never ran.
        assert manage.main(["usage", org_id]) == 0
        out = capsys.readouterr().out
        assert "error" not in out.lower()

    def test_yes_flag_bypasses_the_prompt(self, config, monkeypatch, capsys):
        monkeypatch.setenv("HUB_DATABASE_URL", config.database_url)
        exit_code = manage.main(["purge-org", "00000000-0000-0000-0000-000000000000", "--yes"])
        # Reaches the real function (proven by the *lookup* error, not the
        # confirmation-refused error) -- no prompt, no tty needed.
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "no such organization" in err
        assert "refusing" not in err

    def test_typing_yes_at_the_prompt_proceeds(self, config, monkeypatch, capsys):
        """Simulates a real interactive session: stdin.isatty() reports
        True and input() returns the operator's typed response."""
        monkeypatch.setenv("HUB_DATABASE_URL", config.database_url)
        monkeypatch.setattr(manage.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt: "yes")
        exit_code = manage.main(["purge-org", "00000000-0000-0000-0000-000000000000"])
        assert exit_code == 2  # unknown id -- reached the real lookup, not refused
        err = capsys.readouterr().err
        assert "no such organization" in err

    def test_typing_anything_else_at_the_prompt_refuses(self, config, monkeypatch, capsys):
        monkeypatch.setenv("HUB_DATABASE_URL", config.database_url)
        monkeypatch.setattr(manage.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt: "y")  # not the exact word "yes"
        exit_code = manage.main(["purge-org", "00000000-0000-0000-0000-000000000000"])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "aborted" in err

    def test_ctrl_d_at_the_prompt_aborts_cleanly_instead_of_crashing(self, config, monkeypatch, capsys):
        """Ctrl-D at an interactive prompt raises EOFError from input() --
        an entirely ordinary way to bail out, not an error condition.
        Uncaught, this reached the operator as a raw Python traceback
        instead of the same clean 'aborted' message every other way of
        saying no already gets, and nothing destructive had happened yet
        at the point it was raised."""
        monkeypatch.setenv("HUB_DATABASE_URL", config.database_url)
        monkeypatch.setattr(manage.sys.stdin, "isatty", lambda: True)

        def _raise_eof(prompt):
            raise EOFError()

        monkeypatch.setattr("builtins.input", _raise_eof)
        exit_code = manage.main(["purge-org", "00000000-0000-0000-0000-000000000000"])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "aborted" in err


async def test_argument_count_validation():
    assert manage.main(["purge-trace"]) == 2
    assert manage.main(["purge-trace", "a", "b"]) == 2
    assert manage.main(["list-quarantined", "a", "b"]) == 2  # takes 0 or 1, not 2
    assert manage.main(["approve-submission", "a"]) == 2  # needs a submission id AND an operator org id
    assert manage.main(["reject-submission"]) == 2


async def test_auth_import_is_used():
    # sanity: hub/manage.py's existing key-issuance commands are untouched
    assert auth.generate_raw_key().startswith("ct_live_")


@pytest.mark.filterwarnings("ignore:.*is marked with '@pytest.mark.asyncio'.*:pytest.PytestWarning")
def test_malformed_uuid_reports_a_clean_error_not_a_traceback(config, _schema, monkeypatch, capsys):
    """Regression test for a real bug: `main()` only caught (ValueError,
    LookupError), but a malformed id (`revoke-key not-a-uuid`) is rejected
    by the UUID column type itself -- asyncpg raises that as a driver-level
    error (sqlalchemy.exc.DBAPIError, a SQLAlchemyError, not a ValueError or
    LookupError) that fell through uncaught and dumped a raw traceback for
    the same kind of operator typo the branch above was meant to handle
    cleanly. `main()` builds its own session_factory from HUB_DATABASE_URL
    (not the session_factory fixture) and drives it with asyncio.run(), so
    this has to be a plain sync test -- calling main() from inside a
    already-running async test's event loop would itself raise.
    """
    monkeypatch.setenv("HUB_DATABASE_URL", config.database_url)
    exit_code = manage.main(["revoke-key", "not-a-uuid"])
    assert exit_code == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "Traceback" not in err


@pytest.mark.filterwarnings("ignore:.*is marked with '@pytest.mark.asyncio'.*:pytest.PytestWarning")
def test_main_exits_nonzero_when_a_destructive_op_fails(config, monkeypatch, capsys):
    """The actual bug this fixes: revoke-key/release-quarantine/purge-trace/
    purge-org/commons-seed/set-plan/usage all print "error: ..." to stderr
    and return False on a failed lookup, but nothing about that is a raised
    exception -- there is nothing wrong with the CLI, the id just didn't
    resolve. Before main() checked the command's return value, every one of
    these failures still exited 0, so an automated incident script checking
    $? after e.g. `purge-org <id>` (to confirm a GDPR deletion actually
    happened) would see success on a no-op."""
    monkeypatch.setenv("HUB_DATABASE_URL", config.database_url)
    for command, unknown_id, extra_args in (
        ("revoke-key", "00000000-0000-0000-0000-000000000000", []),
        ("release-quarantine", "00000000-0000-0000-0000-000000000000", []),
        # --yes: this test is pinning the *lookup failure* path (an unknown
        # id must still exit non-zero), not the separate --yes confirmation
        # gate covered by TestPurgeRequiresConfirmation below. Without it,
        # a non-interactive test run (stdin is not a tty) would refuse on
        # the confirmation prompt before ever reaching purge_trace/
        # purge_org, and this test would stop testing what it says it does.
        ("purge-trace", "00000000-0000-0000-0000-000000000000", ["--yes"]),
        ("purge-org", "00000000-0000-0000-0000-000000000000", ["--yes"]),
    ):
        exit_code = manage.main([command, unknown_id, *extra_args])
        assert exit_code == 2, f"{command} on an unknown id must exit non-zero, got {exit_code}"
        assert capsys.readouterr().err.startswith("error:")

    exit_code = manage.main(["set-plan", "00000000-0000-0000-0000-000000000000", "free"])
    assert exit_code == 2
    capsys.readouterr()

    exit_code = manage.main(["set-plan", "00000000-0000-0000-0000-000000000000", "not-a-real-plan"])
    assert exit_code == 2


@pytest.mark.filterwarnings("ignore:.*is marked with '@pytest.mark.asyncio'.*:pytest.PytestWarning")
def test_main_dispatch_treats_only_false_as_failure():
    """Isolates main()'s own dispatch logic (no DB needed): a command
    returning False fails the CLI, True or None (every command with no
    failure path) succeeds."""

    async def _fake_fail(*args):
        return False

    async def _fake_ok(*args):
        return True

    async def _fake_none(*args):
        return None

    original = dict(manage._COMMANDS)
    try:
        manage._COMMANDS["__test_fail__"] = (_fake_fail, 0, 0)
        manage._COMMANDS["__test_ok__"] = (_fake_ok, 0, 0)
        manage._COMMANDS["__test_none__"] = (_fake_none, 0, 0)
        assert manage.main(["__test_fail__"]) == 2
        assert manage.main(["__test_ok__"]) == 0
        assert manage.main(["__test_none__"]) == 0
    finally:
        manage._COMMANDS.clear()
        manage._COMMANDS.update(original)


class TestRetrievalHealthReport:
    """`manage retrieval` is the operator's view of whether search is
    finding anything, on real fleets rather than on the synthetic corpus in
    hub/bench_retrieval.py."""

    async def test_reports_nothing_cleanly_on_an_empty_deployment(self, session_factory, capsys):
        # Explicitly emptied rather than assumed empty: tests that drive
        # manage.main() through HUB_DATABASE_URL create orgs on their own
        # engine, outside this fixture's truncation, so "no orgs exist
        # right now" is an ordering accident and not a property.
        async with session_scope(session_factory) as session:
            for org in (await session.execute(select(Organization))).scalars().all():
                await session.delete(org)
        assert await manage.retrieval(session_factory=session_factory) is True
        assert "no organizations" in capsys.readouterr().out

    async def test_a_miss_and_a_hit_are_both_visible(self, session_factory, config, two_orgs, capsys):
        org_id = two_orgs["org_a"]
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            await crud.contribute_trace(
                session, org_id, config, rate_limiter,
                title="Cache stampede on expiry",
                context_text="many workers recompute the same key at once",
                solution_text="add jitter to the expiry",
                tags=[], agent_type="code", actor="test",
            )
        async with session_scope(session_factory) as session:
            await crud.search_traces(session, org_id, query="stampede")
            await crud.search_traces(session, org_id, query="photosynthesis")

        assert await manage.retrieval(session_factory=session_factory) is True
        out = capsys.readouterr().out
        assert "miss rate" in out
        assert "50%" in out
        # The privacy property is stated in the report itself, not only in a
        # docstring an operator never reads.
        assert "No query text is stored" in out

    async def test_an_unknown_org_is_an_error_not_an_empty_table(self, session_factory, capsys):
        ok = await manage.retrieval(
            "00000000-0000-0000-0000-000000000000", session_factory=session_factory
        )
        assert ok is False
        assert "no such organization" in capsys.readouterr().err


class TestPlanningTheExperimentBeforeStartingIt:
    """`start-experiment` used to take a rate and no guidance, so an operator
    picked one blind. The failure that produces is expensive and silent: the
    fleet runs for a month, the report says "not enough data yet", the window
    is spent, and the only fix -- a wider holdout -- had to be applied at the
    start.

    Planning ON the Hub rather than on paper matters because the Hub already
    knows the numbers: this org's own retrieval volume and its own success
    rate.
    """

    @staticmethod
    async def _with_volume(session_factory, org_id, *, searches, resolved_rate=0.75, n=40):
        from hub.models import Trace

        async with session_scope(session_factory) as session:
            for i in range(n):
                session.add(Trace(
                    org_id=org_id, title=f"password reset problem {i}",
                    context_text="the reset email never arrived for the customer",
                    solution_text="removed the suppression and re-sent",
                    tags=["email"], agent_type="support",
                    outcome={"resolved": (i / n) < resolved_rate},
                ))
        for _ in range(searches):
            async with session_scope(session_factory) as session:
                await crud.search_traces(
                    session, org_id, query="password reset email never arrived")

    async def test_it_reads_the_orgs_own_volume_and_baseline(
        self, session_factory, two_orgs, capsys
    ):
        org_id = two_orgs["org_a"]
        await self._with_volume(session_factory, org_id, searches=30)
        capsys.readouterr()

        await manage.plan_experiment(org_id, "0.10", session_factory=session_factory)
        out = capsys.readouterr().out
        assert "from this org's own recorded outcomes" in out
        assert "from this org's searches this period" in out

    async def test_an_org_with_no_volume_assumes_the_worst(
        self, session_factory, two_orgs, capsys
    ):
        """A plan built on no data must not understate the sample: 50% is
        where the variance peaks."""
        org_id = two_orgs["org_a"]
        capsys.readouterr()
        await manage.plan_experiment(org_id, "0.10", session_factory=session_factory)
        out = capsys.readouterr().out
        assert "most pessimistic" in out
        assert "has not searched yet" in out

    async def test_a_budget_no_rate_can_answer_returns_false(
        self, session_factory, two_orgs, capsys
    ):
        """So an operator script can act on it, and so the exit code says
        what the prose says."""
        org_id = two_orgs["org_a"]
        await self._with_volume(session_factory, org_id, searches=10)
        capsys.readouterr()

        ok = await manage.plan_experiment(org_id, "0.02", session_factory=session_factory)
        assert ok is False
        assert "cannot answer this at any holdout rate" in capsys.readouterr().out

    async def test_an_explicit_window_overrides_the_observed_one(
        self, session_factory, two_orgs, capsys
    ):
        org_id = two_orgs["org_a"]
        await self._with_volume(session_factory, org_id, searches=5)
        capsys.readouterr()

        await manage.plan_experiment(org_id, "0.20", "100000",
                                     session_factory=session_factory)
        out = capsys.readouterr().out
        assert "100,000" in out
        assert "from this org's searches" not in out

    @pytest.mark.parametrize("bad", ["0", "1", "1.5", "abc", "-0.2"])
    async def test_an_impossible_target_is_refused(
        self, session_factory, two_orgs, capsys, bad
    ):
        org_id = two_orgs["org_a"]
        assert await manage.plan_experiment(
            org_id, bad, session_factory=session_factory) is False

    async def test_an_unknown_org_is_refused(self, session_factory, capsys):
        assert await manage.plan_experiment(
            "00000000-0000-0000-0000-000000000000",
            session_factory=session_factory) is False


class TestStartExperimentWarnsAboutAnUnanswerableRate:
    """Said at the only moment the rate can still be changed for free. An
    operator who learns it from the report a month later has spent the
    window, and the fix was always a one-line decision taken now.
    """

    async def test_a_rate_too_low_for_the_volume_warns(
        self, session_factory, two_orgs, capsys
    ):
        org_id = two_orgs["org_a"]
        await TestPlanningTheExperimentBeforeStartingIt._with_volume(
            session_factory, org_id, searches=300)
        capsys.readouterr()

        assert await manage.start_experiment(org_id, "0.05", session_factory=session_factory)
        out = capsys.readouterr().out
        assert "WARNING" in out
        # It still starts: the operator's decision stands, they are told.
        assert "experiment started" in out

    async def test_an_org_with_no_volume_is_not_warned(
        self, session_factory, two_orgs, capsys
    ):
        """Nothing to base a warning on, and inventing one would train
        operators to ignore the real ones."""
        org_id = two_orgs["org_a"]
        capsys.readouterr()
        assert await manage.start_experiment(org_id, "0.2", session_factory=session_factory)
        assert "WARNING" not in capsys.readouterr().out


# --- retention, legal holds and scheduled purge ------------------------------

@pytest_asyncio.fixture
async def aged_org(session_factory):
    """One org with two traces old enough for any sane policy, and one new."""
    from datetime import datetime, timedelta, timezone

    from hub.models import Trace

    now = datetime.now(timezone.utc)
    async with session_scope(session_factory) as session:
        org = Organization(name="retention-fleet")
        session.add(org)
        await session.flush()
        for i, age in enumerate((400, 400, 1)):
            session.add(Trace(
                org_id=org.id, title=f"t{i}", context_text="c", solution_text="s",
                agent_type="support", created_at=now - timedelta(days=age),
            ))
        return org.id


def _digest_from(out: str) -> str:
    """The short plan digest, as an operator would copy it off the screen."""
    for line in out.splitlines():
        if line.startswith("plan "):
            return line.split()[1]
    raise AssertionError(f"no plan digest in output:\n{out}")


class TestRetentionCLI:
    async def test_setting_a_policy_says_nothing_is_deleted_yet(
        self, session_factory, aged_org, capsys
    ):
        ok = await manage.set_retention(
            aged_org, "trace", "90", session_factory=session_factory)
        assert ok
        out = capsys.readouterr().out
        assert "keep 90 days" in out
        # The single most important thing to say at this moment: configuring
        # a policy is not the same act as applying it.
        assert "Nothing is deleted until" in out

    async def test_a_sub_floor_policy_is_refused_with_the_reason(
        self, session_factory, aged_org, capsys
    ):
        ok = await manage.set_retention(
            aged_org, "audit_log", "7", session_factory=session_factory)
        assert not ok
        assert "365" in capsys.readouterr().err

    async def test_a_non_numeric_age_is_an_operator_mistake_not_a_traceback(
        self, session_factory, aged_org, capsys
    ):
        ok = await manage.set_retention(
            aged_org, "trace", "ninety", session_factory=session_factory)
        assert not ok
        assert "whole number" in capsys.readouterr().err

    async def test_plan_prints_what_would_go_and_deletes_nothing(
        self, session_factory, aged_org, capsys
    ):
        from sqlalchemy import func, select

        from hub.models import Trace

        await manage.set_retention(
            aged_org, "trace", "90", session_factory=session_factory)
        capsys.readouterr()
        assert await manage.retention_plan(aged_org, session_factory=session_factory)
        out = capsys.readouterr().out
        assert "2 to delete" in out
        assert "Nothing has been deleted" in out

        async with session_scope(session_factory) as session:
            assert await session.scalar(
                select(func.count()).select_from(Trace)) == 3

    async def test_apply_with_the_printed_digest_deletes(
        self, session_factory, aged_org, capsys
    ):
        from sqlalchemy import func, select

        from hub.models import Trace

        await manage.set_retention(
            aged_org, "trace", "90", session_factory=session_factory)
        capsys.readouterr()
        await manage.retention_plan(aged_org, session_factory=session_factory)
        digest = _digest_from(capsys.readouterr().out)

        assert await manage.retention_apply(
            aged_org, digest, session_factory=session_factory)
        assert "APPLIED. 2 rows deleted." in capsys.readouterr().out
        async with session_scope(session_factory) as session:
            assert await session.scalar(
                select(func.count()).select_from(Trace)) == 1

    async def test_a_stale_digest_is_refused_and_shows_the_new_one(
        self, session_factory, aged_org, capsys
    ):
        from sqlalchemy import func, select

        from hub.models import Trace

        await manage.set_retention(
            aged_org, "trace", "90", session_factory=session_factory)
        capsys.readouterr()
        await manage.retention_plan(aged_org, session_factory=session_factory)
        stale = _digest_from(capsys.readouterr().out)

        # The world moves between reading the plan and approving it: another
        # old trace arrives, so the approved set is no longer the real one.
        from datetime import datetime, timedelta, timezone
        async with session_scope(session_factory) as session:
            session.add(Trace(
                org_id=aged_org, title="late arrival", context_text="c",
                solution_text="s", agent_type="support",
                created_at=datetime.now(timezone.utc) - timedelta(days=500),
            ))
        capsys.readouterr()

        assert not await manage.retention_apply(
            aged_org, stale, session_factory=session_factory)
        err = capsys.readouterr().err
        assert "nothing was deleted" in err
        assert "you approved" in err and "current plan" in err
        async with session_scope(session_factory) as session:
            assert await session.scalar(
                select(func.count()).select_from(Trace)) == 4

    async def test_the_digest_commits_to_rows_not_to_the_policy_text(
        self, session_factory, aged_org, capsys
    ):
        """Tightening 90d to 30d dooms the same two 400-day-old traces, so
        the approval is still accurate and the apply proceeds. The digest
        deliberately commits to the CONSEQUENCES an operator read, not to
        the configuration that produced them -- a change that does not move
        a single row has not invalidated their approval, and refusing it
        would train operators to re-approve reflexively."""
        from sqlalchemy import func, select

        from hub.models import Trace

        await manage.set_retention(
            aged_org, "trace", "90", session_factory=session_factory)
        capsys.readouterr()
        await manage.retention_plan(aged_org, session_factory=session_factory)
        digest = _digest_from(capsys.readouterr().out)

        await manage.set_retention(
            aged_org, "trace", "30", session_factory=session_factory)
        capsys.readouterr()

        assert await manage.retention_apply(
            aged_org, digest, session_factory=session_factory)
        capsys.readouterr()
        async with session_scope(session_factory) as session:
            assert await session.scalar(
                select(func.count()).select_from(Trace)) == 1

    async def test_a_legal_hold_survives_an_apply(
        self, session_factory, aged_org, capsys
    ):
        from sqlalchemy import func, select

        from hub.models import Trace

        await manage.set_retention(
            aged_org, "trace", "90", session_factory=session_factory)
        assert await manage.place_legal_hold(
            aged_org, "Ohio subpoena 2026-44", session_factory=session_factory)
        capsys.readouterr()

        await manage.retention_plan(aged_org, session_factory=session_factory)
        out = capsys.readouterr().out
        digest = _digest_from(out)
        assert "Ohio subpoena 2026-44" in out

        await manage.retention_apply(
            aged_org, digest, session_factory=session_factory)
        capsys.readouterr()
        async with session_scope(session_factory) as session:
            assert await session.scalar(
                select(func.count()).select_from(Trace)) == 3

    async def test_a_hold_without_a_reason_is_refused(
        self, session_factory, aged_org, capsys
    ):
        assert not await manage.place_legal_hold(
            aged_org, "  ", session_factory=session_factory)
        assert "reason" in capsys.readouterr().err

    async def test_holds_lists_policies_and_freezes(
        self, session_factory, aged_org, capsys
    ):
        await manage.set_retention(
            aged_org, "trace", "90", session_factory=session_factory)
        await manage.place_legal_hold(
            aged_org, "investigation", session_factory=session_factory)
        capsys.readouterr()
        assert await manage.list_legal_holds(aged_org, session_factory=session_factory)
        out = capsys.readouterr().out
        assert "trace [any]: keep 90d" in out
        assert "investigation" in out

    async def test_holds_says_plainly_when_nothing_expires(
        self, session_factory, aged_org, capsys
    ):
        assert await manage.list_legal_holds(aged_org, session_factory=session_factory)
        out = capsys.readouterr().out
        assert "nothing expires on its own" in out
        assert "No legal holds in force" in out

    async def test_clearing_a_policy_stops_expiry(
        self, session_factory, aged_org, capsys
    ):
        await manage.set_retention(
            aged_org, "trace", "90", session_factory=session_factory)
        assert await manage.clear_retention(
            aged_org, "trace", session_factory=session_factory)
        capsys.readouterr()
        await manage.retention_plan(aged_org, session_factory=session_factory)
        assert "indefinitely" in capsys.readouterr().out

    async def test_every_retention_command_rejects_an_unknown_org(
        self, session_factory, capsys
    ):
        missing = "00000000-0000-0000-0000-000000000000"
        assert not await manage.set_retention(
            missing, "trace", "90", session_factory=session_factory)
        assert not await manage.retention_plan(missing, session_factory=session_factory)
        assert not await manage.place_legal_hold(
            missing, "r", session_factory=session_factory)


class TestRetentionCommandTable:
    """The dispatch table and the module docstring are what an operator
    actually reads; a command that exists but is unreachable or undocumented
    is not shipped."""

    async def test_every_retention_command_is_dispatchable(self):
        for name in ("set-retention", "clear-retention", "retention-plan",
                     "retention-apply", "legal-hold", "release-hold", "holds"):
            assert name in manage._COMMANDS, name

    async def test_every_retention_command_is_documented(self):
        for name in ("set-retention", "clear-retention", "retention-plan",
                     "retention-apply", "legal-hold", "release-hold", "holds"):
            assert name in manage.__doc__, name

    async def test_apply_is_not_gated_on_an_interactive_prompt(self):
        """Its confirmation is the plan digest, which names the exact rows
        and refuses if anything moved. A prompt on top would add no safety
        and would make the scheduled purge impossible to automate -- which
        is the whole point of a retention policy rather than a delete
        button."""
        assert "retention-apply" not in manage._DESTRUCTIVE_COMMANDS


# --- webhook event export ----------------------------------------------------

class TestWebhookCLI:
    URL = "https://example.invalid/hooks/commontrace"

    @pytest_asyncio.fixture
    async def hooked(self, session_factory, monkeypatch, capsys):
        """One org with one endpoint, and the secret the CLI printed."""
        from hub import manage as manage_mod

        monkeypatch.setattr(manage_mod, "_config_signing_key", lambda: "test-key")
        async with session_scope(session_factory) as session:
            org = Organization(name="hooked")
            session.add(org)
            await session.flush()
            org_id = org.id
        assert await manage.webhook_add(
            org_id, self.URL, session_factory=session_factory)
        out = capsys.readouterr().out
        secret = next(
            line.split(": ", 1)[1].strip()
            for line in out.splitlines() if "signing secret" in line
        )
        endpoint_id = out.splitlines()[0].split()[1]
        return {"org": org_id, "id": endpoint_id, "secret": secret, "out": out}

    async def test_adding_prints_the_secret_once_and_says_it_is_not_stored(
        self, hooked
    ):
        assert len(hooked["secret"]) == 64  # sha256 hex
        # The operator has to be told they cannot read it back, at the one
        # moment they could still copy it.
        assert "shown ONCE" in hooked["out"]
        assert "not stored" in hooked["out"]

    async def test_the_secret_really_cannot_be_read_back(
        self, session_factory, hooked
    ):
        """Not just "we do not print it again" -- it is not in the row."""
        from hub.models import WebhookEndpoint

        async with session_scope(session_factory) as session:
            row = await session.get(WebhookEndpoint, hooked["id"])
            stored = " ".join(
                str(getattr(row, c.name)) for c in row.__table__.columns
            )
        assert hooked["secret"] not in stored

    async def test_plaintext_http_is_refused(self, session_factory, hooked, capsys):
        assert not await manage.webhook_add(
            hooked["org"], "http://example.invalid/h", session_factory=session_factory)
        assert "https" in capsys.readouterr().err

    async def test_an_unknown_org_is_refused(self, session_factory, capsys):
        assert not await manage.webhook_add(
            "00000000-0000-0000-0000-000000000000", self.URL,
            session_factory=session_factory)
        assert "no such organization" in capsys.readouterr().err

    async def test_the_audit_row_records_the_url_and_never_the_secret(
        self, session_factory, hooked
    ):
        from sqlalchemy import select

        from hub.models import AuditLogEntry

        async with session_scope(session_factory) as session:
            entry = (await session.execute(
                select(AuditLogEntry).where(AuditLogEntry.action == "webhook.add")
            )).scalar_one()
        assert self.URL in entry.summary
        assert hooked["secret"] not in entry.summary

    async def test_listing_shows_the_endpoint_and_the_queue(
        self, session_factory, hooked, capsys
    ):
        capsys.readouterr()
        assert await manage.webhook_list(hooked["org"], session_factory=session_factory)
        out = capsys.readouterr().out
        assert self.URL in out
        assert "enabled" in out
        assert "pending" in out

    async def test_listing_an_org_with_nothing_says_so_plainly(
        self, session_factory, capsys
    ):
        async with session_scope(session_factory) as session:
            org = Organization(name="quiet")
            session.add(org)
            await session.flush()
            org_id = org.id
        assert await manage.webhook_list(org_id, session_factory=session_factory)
        assert "Nothing is told anything" in capsys.readouterr().out

    async def test_rotating_prints_a_different_secret(
        self, session_factory, hooked, capsys
    ):
        capsys.readouterr()
        assert await manage.webhook_rotate(
            hooked["id"], session_factory=session_factory)
        out = capsys.readouterr().out
        assert hooked["secret"] not in out
        assert "stops verifying immediately" in out

    async def test_disabling_stops_future_queueing(
        self, session_factory, hooked, capsys
    ):
        from sqlalchemy import func, select

        from hub import events
        from hub.models import WebhookDelivery

        assert await manage.webhook_disable(
            hooked["id"], session_factory=session_factory)
        async with session_scope(session_factory) as session:
            await events.emit(
                session, hooked["org"], "trace.created", {"trace_id": "t1"})
        async with session_scope(session_factory) as session:
            assert await session.scalar(
                select(func.count()).select_from(WebhookDelivery)) == 0

    async def test_a_non_numeric_limit_is_an_operator_mistake(
        self, session_factory, capsys
    ):
        assert not await manage.webhook_deliver(
            "lots", session_factory=session_factory)
        assert "whole number" in capsys.readouterr().err

    async def test_starting_an_experiment_announces_it(
        self, session_factory, hooked
    ):
        from sqlalchemy import select

        from hub.models import WebhookDelivery

        await manage.start_experiment(hooked["org"], "0.5", session_factory=session_factory)
        async with session_scope(session_factory) as session:
            rows = list((await session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.event_type == "experiment.started")
            )).scalars())
        assert len(rows) == 1
        assert rows[0].payload["rate"] == 0.5

    async def test_stopping_an_experiment_announces_it(
        self, session_factory, hooked
    ):
        from sqlalchemy import select

        from hub.models import WebhookDelivery

        await manage.start_experiment(hooked["org"], "0.5", session_factory=session_factory)
        await manage.stop_experiment(hooked["org"], session_factory=session_factory)
        async with session_scope(session_factory) as session:
            rows = list((await session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.event_type == "experiment.stopped")
            )).scalars())
        assert len(rows) == 1


class TestWebhookCommandTable:
    async def test_every_webhook_command_is_dispatchable_and_documented(self):
        for name in ("webhook-add", "webhook-list", "webhook-rotate",
                     "webhook-disable", "webhook-deliver"):
            assert name in manage._COMMANDS, name
            assert name in manage.__doc__, name


class TestUserCLI:
    @pytest_asyncio.fixture
    async def org_id(self, session_factory):
        async with session_scope(session_factory) as session:
            org = Organization(name="user-cli-org")
            session.add(org)
            await session.flush()
            return org.id

    async def test_creating_a_user_prints_the_id_and_says_sso_is_not_linked(
        self, session_factory, org_id, capsys
    ):
        assert await manage.create_user(
            org_id, "ana@example.com", rbac.ROLE_ANALYST, session_factory=session_factory)
        out = capsys.readouterr().out
        assert "ana@example.com" in out
        assert "cannot sign in" in out

    async def test_an_unknown_role_is_refused(self, session_factory, org_id, capsys):
        assert not await manage.create_user(
            org_id, "ana@example.com", "supreme-leader", session_factory=session_factory)
        assert "supreme-leader" in capsys.readouterr().err

    async def test_an_unknown_org_is_refused(self, session_factory, capsys):
        assert not await manage.create_user(
            "00000000-0000-0000-0000-000000000000", "ana@example.com",
            rbac.ROLE_ANALYST, session_factory=session_factory)
        assert "no such organization" in capsys.readouterr().err

    async def test_a_second_user_with_the_same_email_in_the_same_org_is_refused(
        self, session_factory, org_id, capsys
    ):
        assert await manage.create_user(
            org_id, "dup@example.com", rbac.ROLE_VIEWER, session_factory=session_factory)
        capsys.readouterr()
        assert not await manage.create_user(
            org_id, "dup@example.com", rbac.ROLE_ANALYST, session_factory=session_factory)
        assert "already has a user" in capsys.readouterr().err

    async def test_the_same_email_is_fine_in_a_different_org(
        self, session_factory, org_id, capsys
    ):
        async with session_scope(session_factory) as session:
            other_org = Organization(name="other-org")
            session.add(other_org)
            await session.flush()
            other_org_id = other_org.id
        assert await manage.create_user(
            org_id, "shared@example.com", rbac.ROLE_VIEWER, session_factory=session_factory)
        assert await manage.create_user(
            other_org_id, "shared@example.com", rbac.ROLE_VIEWER,
            session_factory=session_factory)

    async def test_listing_an_empty_org_says_so_plainly(
        self, session_factory, org_id, capsys
    ):
        assert await manage.list_users(org_id, session_factory=session_factory)
        assert "No users for" in capsys.readouterr().out

    async def test_listing_shows_role_state_and_sso_link_status(
        self, session_factory, org_id, capsys
    ):
        assert await manage.create_user(
            org_id, "ana@example.com", rbac.ROLE_CURATOR, session_factory=session_factory)
        capsys.readouterr()
        assert await manage.list_users(org_id, session_factory=session_factory)
        out = capsys.readouterr().out
        assert "ana@example.com" in out
        assert f"role={rbac.ROLE_CURATOR}" in out
        assert "active" in out
        assert "no SSO linked" in out

    async def test_an_unknown_org_is_refused_when_listing(self, session_factory, capsys):
        assert not await manage.list_users(
            "00000000-0000-0000-0000-000000000000", session_factory=session_factory)
        assert "no such organization" in capsys.readouterr().err

    async def test_setting_the_role_takes_effect_immediately(
        self, session_factory, org_id, capsys
    ):
        await manage.create_user(
            org_id, "ana@example.com", rbac.ROLE_VIEWER, session_factory=session_factory)
        out = capsys.readouterr().out
        user_id = out.splitlines()[0].split()[1]
        capsys.readouterr()
        assert await manage.set_user_role(
            user_id, rbac.ROLE_DEPLOYER, session_factory=session_factory)
        assert "viewer -> deployer" in capsys.readouterr().out
        async with session_scope(session_factory) as session:
            row = await session.get(User, user_id)
            assert row.role == rbac.ROLE_DEPLOYER

    async def test_setting_an_unknown_role_is_refused(
        self, session_factory, org_id, capsys
    ):
        await manage.create_user(
            org_id, "ana@example.com", rbac.ROLE_VIEWER, session_factory=session_factory)
        out = capsys.readouterr().out
        user_id = out.splitlines()[0].split()[1]
        capsys.readouterr()
        assert not await manage.set_user_role(
            user_id, "supreme-leader", session_factory=session_factory)
        assert "supreme-leader" in capsys.readouterr().err

    async def test_setting_the_role_of_an_unknown_user_is_refused(
        self, session_factory, capsys
    ):
        assert not await manage.set_user_role(
            "00000000-0000-0000-0000-000000000000", rbac.ROLE_VIEWER,
            session_factory=session_factory)
        assert "no such user" in capsys.readouterr().err

    async def test_disabling_blocks_access_and_is_idempotent_refused_on_repeat(
        self, session_factory, org_id, capsys
    ):
        await manage.create_user(
            org_id, "ana@example.com", rbac.ROLE_VIEWER, session_factory=session_factory)
        out = capsys.readouterr().out
        user_id = out.splitlines()[0].split()[1]
        capsys.readouterr()
        assert await manage.disable_user(user_id, session_factory=session_factory)
        assert "disabled" in capsys.readouterr().out
        async with session_scope(session_factory) as session:
            row = await session.get(User, user_id)
            assert row.disabled_at is not None
        assert not await manage.disable_user(user_id, session_factory=session_factory)
        assert "already disabled" in capsys.readouterr().err

    async def test_disabling_an_unknown_user_is_refused(self, session_factory, capsys):
        assert not await manage.disable_user(
            "00000000-0000-0000-0000-000000000000", session_factory=session_factory)
        assert "no such user" in capsys.readouterr().err

    async def test_enabling_clears_the_disabled_flag(
        self, session_factory, org_id, capsys
    ):
        await manage.create_user(
            org_id, "ana@example.com", rbac.ROLE_VIEWER, session_factory=session_factory)
        out = capsys.readouterr().out
        user_id = out.splitlines()[0].split()[1]
        await manage.disable_user(user_id, session_factory=session_factory)
        capsys.readouterr()
        assert await manage.enable_user(user_id, session_factory=session_factory)
        assert "re-enabled" in capsys.readouterr().out
        async with session_scope(session_factory) as session:
            row = await session.get(User, user_id)
            assert row.disabled_at is None

    async def test_enabling_a_user_that_is_not_disabled_is_refused(
        self, session_factory, org_id, capsys
    ):
        await manage.create_user(
            org_id, "ana@example.com", rbac.ROLE_VIEWER, session_factory=session_factory)
        out = capsys.readouterr().out
        user_id = out.splitlines()[0].split()[1]
        capsys.readouterr()
        assert not await manage.enable_user(user_id, session_factory=session_factory)
        assert "not disabled" in capsys.readouterr().err

    async def test_enabling_an_unknown_user_is_refused(self, session_factory, capsys):
        assert not await manage.enable_user(
            "00000000-0000-0000-0000-000000000000", session_factory=session_factory)
        assert "no such user" in capsys.readouterr().err

    async def test_linking_sso_lets_the_user_authenticate(
        self, session_factory, org_id, capsys
    ):
        await manage.create_user(
            org_id, "ana@example.com", rbac.ROLE_VIEWER, session_factory=session_factory)
        out = capsys.readouterr().out
        user_id = out.splitlines()[0].split()[1]
        capsys.readouterr()
        assert await manage.link_sso(
            user_id, "https://idp.example.com", "sub-123", session_factory=session_factory)
        assert "idp.example.com" in capsys.readouterr().out
        async with session_scope(session_factory) as session:
            row = await session.get(User, user_id)
            assert row.issuer == "https://idp.example.com"
            assert row.external_subject == "sub-123"

    async def test_the_same_issuer_and_subject_cannot_be_linked_to_two_users(
        self, session_factory, org_id, capsys
    ):
        await manage.create_user(
            org_id, "ana@example.com", rbac.ROLE_VIEWER, session_factory=session_factory)
        out = capsys.readouterr().out
        first_id = out.splitlines()[0].split()[1]
        await manage.create_user(
            org_id, "bea@example.com", rbac.ROLE_VIEWER, session_factory=session_factory)
        out = capsys.readouterr().out
        second_id = out.splitlines()[0].split()[1]

        assert await manage.link_sso(
            first_id, "https://idp.example.com", "sub-123", session_factory=session_factory)
        capsys.readouterr()
        assert not await manage.link_sso(
            second_id, "https://idp.example.com", "sub-123",
            session_factory=session_factory)
        assert "already linked" in capsys.readouterr().err

    async def test_linking_an_unknown_user_is_refused(self, session_factory, capsys):
        assert not await manage.link_sso(
            "00000000-0000-0000-0000-000000000000", "https://idp.example.com",
            "sub-123", session_factory=session_factory)
        assert "no such user" in capsys.readouterr().err

    async def test_unlinking_removes_the_identity_but_keeps_the_row(
        self, session_factory, org_id, capsys
    ):
        await manage.create_user(
            org_id, "ana@example.com", rbac.ROLE_VIEWER, session_factory=session_factory)
        out = capsys.readouterr().out
        user_id = out.splitlines()[0].split()[1]
        await manage.link_sso(
            user_id, "https://idp.example.com", "sub-123", session_factory=session_factory)
        capsys.readouterr()
        assert await manage.unlink_sso(user_id, session_factory=session_factory)
        assert "unlinked" in capsys.readouterr().out
        async with session_scope(session_factory) as session:
            row = await session.get(User, user_id)
            assert row.external_subject == ""
            assert row.role == rbac.ROLE_VIEWER

    async def test_unlinking_a_user_with_nothing_linked_is_refused(
        self, session_factory, org_id, capsys
    ):
        await manage.create_user(
            org_id, "ana@example.com", rbac.ROLE_VIEWER, session_factory=session_factory)
        out = capsys.readouterr().out
        user_id = out.splitlines()[0].split()[1]
        capsys.readouterr()
        assert not await manage.unlink_sso(user_id, session_factory=session_factory)
        assert "no SSO identity" in capsys.readouterr().err

    async def test_unlinking_an_unknown_user_is_refused(self, session_factory, capsys):
        assert not await manage.unlink_sso(
            "00000000-0000-0000-0000-000000000000", session_factory=session_factory)
        assert "no such user" in capsys.readouterr().err


class TestUserCommandTable:
    async def test_every_user_command_is_dispatchable_and_documented(self):
        for name in ("create-user", "list-users", "set-user-role", "disable-user",
                     "enable-user", "link-sso", "unlink-sso"):
            assert name in manage._COMMANDS, name
            assert name in manage.__doc__, name


class TestAlertCLI:
    @pytest_asyncio.fixture
    async def org_id(self, session_factory):
        async with session_scope(session_factory) as session:
            org = Organization(name="alert-cli-org")
            session.add(org)
            await session.flush()
            return org.id

    async def test_creating_a_rule_prints_its_id(self, session_factory, org_id, capsys):
        assert await manage.create_alert_rule(
            org_id, "quarantine_rate", "gt", "10", session_factory=session_factory)
        out = capsys.readouterr().out
        assert "quarantine_rate gt 10.0" in out

    async def test_a_non_numeric_threshold_is_an_operator_mistake(
        self, session_factory, org_id, capsys
    ):
        assert not await manage.create_alert_rule(
            org_id, "quarantine_rate", "gt", "lots", session_factory=session_factory)
        assert "number" in capsys.readouterr().err

    async def test_an_unknown_metric_is_refused(self, session_factory, org_id, capsys):
        assert not await manage.create_alert_rule(
            org_id, "vibes", "gt", "10", session_factory=session_factory)
        assert "unknown metric" in capsys.readouterr().err

    async def test_listing_an_empty_org_says_so_plainly(
        self, session_factory, org_id, capsys
    ):
        assert await manage.list_alert_rules(org_id, session_factory=session_factory)
        assert "No alert rules" in capsys.readouterr().out

    async def test_an_unknown_org_is_refused_when_listing(self, session_factory, capsys):
        assert not await manage.list_alert_rules(
            "00000000-0000-0000-0000-000000000000", session_factory=session_factory)
        assert "no such organization" in capsys.readouterr().err

    async def test_listing_shows_metric_and_state(
        self, session_factory, org_id, capsys
    ):
        await manage.create_alert_rule(
            org_id, "quarantine_rate", "gt", "10", session_factory=session_factory)
        capsys.readouterr()
        assert await manage.list_alert_rules(org_id, session_factory=session_factory)
        out = capsys.readouterr().out
        assert "quarantine_rate gt 10.0" in out
        assert "enabled" in out

    async def test_deleting_a_rule(self, session_factory, org_id, capsys):
        await manage.create_alert_rule(
            org_id, "quarantine_rate", "gt", "10", session_factory=session_factory)
        out = capsys.readouterr().out
        rule_id = out.splitlines()[0].split()[1]
        capsys.readouterr()
        assert await manage.delete_alert_rule(rule_id, session_factory=session_factory)
        assert "deleted" in capsys.readouterr().out

    async def test_deleting_an_unknown_rule_is_refused(self, session_factory, capsys):
        assert not await manage.delete_alert_rule(
            "00000000-0000-0000-0000-000000000000", session_factory=session_factory)
        assert "no such alert rule" in capsys.readouterr().err

    async def test_check_alerts_with_nothing_crossed_says_so(
        self, session_factory, org_id, capsys
    ):
        await manage.create_alert_rule(
            org_id, "quarantine_rate", "gt", "10", session_factory=session_factory)
        capsys.readouterr()
        assert await manage.check_alerts(org_id, session_factory=session_factory)
        assert "No alert crossed" in capsys.readouterr().out

    async def test_check_alerts_across_every_org_when_none_given(
        self, session_factory, capsys
    ):
        assert await manage.check_alerts(session_factory=session_factory)
        assert "No alert crossed" in capsys.readouterr().out

    async def test_generate_report_prints_a_summary(
        self, session_factory, org_id, capsys
    ):
        assert await manage.generate_report(org_id, session_factory=session_factory)
        out = capsys.readouterr().out
        assert "report.generated" in out
        assert "traces=0" in out

    async def test_generate_report_unknown_org_is_refused(self, session_factory, capsys):
        assert not await manage.generate_report(
            "00000000-0000-0000-0000-000000000000", session_factory=session_factory)
        assert "no such organization" in capsys.readouterr().err


class TestAlertCommandTable:
    async def test_every_alert_command_is_dispatchable_and_documented(self):
        for name in ("create-alert-rule", "list-alert-rules", "delete-alert-rule",
                     "check-alerts", "generate-report"):
            assert name in manage._COMMANDS, name
            assert name in manage.__doc__, name


class TestSearchContentCLI:
    async def test_finds_a_match_and_says_it_deletes_nothing(
        self, session_factory, two_orgs, capsys
    ):
        async with session_scope(session_factory) as session:
            session.add(Trace(
                org_id=two_orgs["org_a"], title="t", context_text="jane.smith@example.com",
                solution_text="s", agent_type="support",
            ))
        assert await manage.search_content(
            two_orgs["org_a"], "jane.smith@example.com", session_factory=session_factory)
        out = capsys.readouterr().out
        assert "jane.smith@example.com" in out
        assert "deletes nothing" in out

    async def test_no_match_says_so_plainly(self, session_factory, two_orgs, capsys):
        assert await manage.search_content(
            two_orgs["org_a"], "no-such-identifier", session_factory=session_factory)
        assert "no traces" in capsys.readouterr().out

    async def test_an_unknown_org_is_refused(self, session_factory, capsys):
        assert not await manage.search_content(
            "00000000-0000-0000-0000-000000000000", "x", session_factory=session_factory)
        assert "no such organization" in capsys.readouterr().err

    async def test_an_invalid_mode_is_an_operator_mistake(self, session_factory, two_orgs, capsys):
        assert not await manage.search_content(
            two_orgs["org_a"], "x", "fuzzy", session_factory=session_factory)
        assert "literal" in capsys.readouterr().err

    async def test_an_invalid_regex_is_refused_cleanly(self, session_factory, two_orgs, capsys):
        assert not await manage.search_content(
            two_orgs["org_a"], "(unbalanced(", "regex", session_factory=session_factory)
        assert "not a valid regular expression" in capsys.readouterr().err

    async def test_search_content_is_dispatchable_and_documented(self):
        assert "search-content" in manage._COMMANDS
        assert "search-content" in manage.__doc__


class TestSubjectTaggingCLI:
    async def test_tag_then_find_then_purge_round_trip(self, session_factory, two_orgs, capsys):
        async with session_scope(session_factory) as session:
            trace = Trace(
                org_id=two_orgs["org_a"], title="t", context_text="c", solution_text="s",
                agent_type="support",
            )
            session.add(trace)
            await session.flush()
            trace_id = trace.id

        assert await manage.tag_trace_subjects(
            two_orgs["org_a"], trace_id, "user-42", session_factory=session_factory,
        )
        assert "tagged with 1 subject" in capsys.readouterr().out

        assert await manage.find_subject_traces(
            two_orgs["org_a"], "user-42", session_factory=session_factory,
        )
        out = capsys.readouterr().out
        assert trace_id in out
        assert "exact match" in out

        assert await manage.purge_subject_traces(
            two_orgs["org_a"], "user-42", session_factory=session_factory,
        )
        assert "purged 1 trace" in capsys.readouterr().out

        async with session_factory() as session:
            assert await session.get(Trace, trace_id) is None

    async def test_find_with_no_match_says_so_plainly(self, session_factory, two_orgs, capsys):
        assert await manage.find_subject_traces(
            two_orgs["org_a"], "no-such-subject", session_factory=session_factory,
        )
        assert "no traces" in capsys.readouterr().out

    async def test_purge_with_no_match_purges_zero_not_an_error(self, session_factory, two_orgs, capsys):
        assert await manage.purge_subject_traces(
            two_orgs["org_a"], "no-such-subject", session_factory=session_factory,
        )
        assert "purged 0 trace" in capsys.readouterr().out

    async def test_tagging_an_unknown_trace_is_refused(self, session_factory, two_orgs, capsys):
        assert not await manage.tag_trace_subjects(
            two_orgs["org_a"], "00000000-0000-0000-0000-000000000000", "user-42",
            session_factory=session_factory,
        )
        assert "no trace" in capsys.readouterr().err

    async def test_empty_subject_ids_csv_clears_the_tag(self, session_factory, two_orgs, capsys):
        async with session_scope(session_factory) as session:
            trace = Trace(
                org_id=two_orgs["org_a"], title="t", context_text="c", solution_text="s",
                agent_type="support",
            )
            session.add(trace)
            await session.flush()
            trace_id = trace.id
        assert await manage.tag_trace_subjects(
            two_orgs["org_a"], trace_id, "user-42", session_factory=session_factory,
        )
        assert await manage.tag_trace_subjects(
            two_orgs["org_a"], trace_id, "", session_factory=session_factory,
        )
        assert "tagged with 0 subject" in capsys.readouterr().out

    async def test_subject_tagging_commands_are_dispatchable_and_documented(self):
        for command in ("tag-trace-subjects", "find-subject-traces", "purge-subject-traces"):
            assert command in manage._COMMANDS
            assert command in manage.__doc__
