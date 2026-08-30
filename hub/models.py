"""SQLAlchemy 2.0 ORM models for the Hub's Postgres store.

Tenant isolation note (see hub/README.md "Tenant isolation" section for the
full design rationale): every row that can carry org-specific content has a
non-nullable, indexed `org_id`. Read paths in hub/crud.py filter by org_id
*in the SQL WHERE clause*, never by fetching rows and filtering in Python —
that is the property hub/tests/test_tenant_isolation.py asserts.

Only `Trace` is stored here, not `Lesson`. This is deliberate, not an
oversight: per protocol/PROTOCOL.md §4, "Lessons are local-store scaffolding
... the Curator/Validator roles turn Traces into Lessons before promoting the
durable, reusable half of a Lesson to a Hub Trace via contribute_trace." None
of the six Hub MCP tools (search_traces, contribute_trace, get_trace,
vote_trace, amend_trace, list_tags) accept or return a Lesson-shaped object,
so a `lessons` table would be dead schema. hub/schema_validation.py still
loads lesson.schema.json from disk (so the validation infrastructure is
generic, and so a future Lesson-bearing tool doesn't require re-plumbing),
it just isn't exercised by any current write path.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Postgres text-search configuration used for the traces.search_vector
# generated column and for every query that matches against it. The two MUST
# agree -- a tsvector built with 'english' and a tsquery built with a
# different config will silently fail to match. Keep this the single source
# of truth rather than repeating the literal in models.py + crud.py.
#
# Note this must be a *literal* config name, not the 1-arg to_tsvector():
# a GENERATED column's expression has to be IMMUTABLE, and 1-arg
# to_tsvector() depends on the session's default_text_search_config, which
# makes it merely STABLE and Postgres rejects it here.
TEXT_SEARCH_CONFIG = "english"


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    # Which entitlements this org has (hub/plans.py). Stored as the plan
    # NAME rather than as the limits themselves, so that changing what a
    # plan grants is a code change reviewed once, not a data migration over
    # every customer row that can silently half-apply. An unrecognized name
    # resolves to the smallest plan, never an unlimited one -- see
    # hub/plans.py:get.
    plan: Mapped[str] = mapped_column(String(32), default="free", nullable=False)
    # Permanent addition to this org's monthly Knowledge Base query
    # allowance (hub/plans.py:query_allowance), earned one
    # hub/manage.py review-submission approval at a time -- never by the
    # act of submitting. That is what keeps this from being the same
    # credit-for-volume mechanic STRATEGY.md §3 already ruled out: a
    # rejected or ignored KnowledgeBaseSubmission earns nothing, so the
    # only way to raise this number is to write something an operator
    # judged worth publishing.
    bonus_commons_queries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # --- Randomized holdout configuration ------------------------------
    #
    # The fraction of (trace, occasion) pairs whose retrieved memory is
    # deliberately WITHHELD, so the fleet's own outcomes can be compared
    # against a control arm the fleet itself generated. 0.0 -- the default
    # -- means no experiment is running and nothing is ever withheld.
    #
    # Held here, per org, rather than passed per call, because a fleet is
    # many agents and an experiment is only coherent if every one of them
    # draws from the SAME randomization. An agent that computed its own
    # assignment with its own rate would put the same lesson in both arms
    # on different machines, which does not fail loudly -- it quietly
    # produces a comparison of two mixtures and an effect estimate biased
    # toward zero.
    holdout_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Names this experiment. commontrace.experiment.is_held_out hashes it
    # with the trace id and occasion id, so changing it reshuffles every
    # assignment -- which is why it is written once when an experiment
    # starts and never edited. Rotating it mid-flight silently mixes two
    # different randomizations into one comparison, and the result looks
    # like ordinary noise rather than like a broken experiment.
    holdout_salt: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    # --- Self-service account deletion (hub/crud.py:request_org_deletion) --
    #
    # A two-call design, deliberately: `request_account_deletion` alone
    # never deletes anything, it only stores a hashed confirmation token and
    # a window during which `confirm_account_deletion` may present the raw
    # token to actually execute the purge. This answers the question
    # hub/README.md's Operator CLI section originally left open --
    # "should a single compromised key be able to wipe an org's entire
    # trace history with no confirmation step?" -- with no: a second,
    # differently-named call is required, and it cannot succeed before
    # `crud.DELETION_GRACE_SECONDS` has elapsed, which is deliberately long
    # enough for an operator watching the audit log (every request is
    # recorded there) to revoke a compromised key first via
    # `hub.manage revoke-key`. The raw token itself is never stored, only
    # its hash -- same pattern as ApiKey.key_hash.
    deletion_token_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    deletion_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deletion_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="organization", cascade="all, delete-orphan")


class ApiKey(Base):
    """An API key belongs to exactly one org. The raw key is never stored —
    only its argon2 hash, plus a short non-secret prefix so an operator can
    identify a key in logs/UI without ever reconstructing it."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # NULL = never expires (the pre-existing behavior, and still the default
    # for a key issued without a day count). A non-NULL value is enforced
    # at verification time in hub/auth.py, so an expired key stops working
    # without anyone having to run a revocation job.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped[Organization] = relationship(back_populates="api_keys")


class Trace(Base):
    __tablename__ = "traces"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Core Trace fields (trace.schema.json) -----------------------------
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    context_text: Mapped[str] = mapped_column(Text, nullable=False)
    solution_text: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(128)), default=list, nullable=False)
    agent_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # The AGENT, as distinct from the KIND of agent above. agent_type is a
    # category ("support", "sales", "code"): a fleet of 25 support agents
    # shares one value, so it can never answer "how many agents does this
    # org run" -- the variable STRATEGY.md §12.6 concludes the business
    # should be run on, and which nothing in this system could compute
    # before this column existed.
    #
    # Empty string, not NULL, for "the client did not say" -- matching
    # profile/contributor above, and letting the migration backfill every
    # pre-existing row with a server_default rather than leaving NULLs that
    # every COUNT(DISTINCT ...) would then have to special-case. The
    # counting layer maps "" to plans.UNATTRIBUTED_AGENT_ID exactly once
    # (hub/crud.py:agents_under_management), so the magic value lives in
    # one place instead of in every row.
    agent_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    profile: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    extensions: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    watch_condition: Mapped[str] = mapped_column(Text, default="", nullable=False)
    review_after: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    # Not a ForeignKey: the trace it supersedes may already be purged (see
    # hub/manage.py:purge_trace's amendment-chain walk), and a dangling FK
    # would block that deletion rather than let the chain be cleaned up.
    # Indexed anyway -- amend_trace's chain walk and any lookup of "what
    # superseded this trace" filters on it, and without an index that is a
    # full table scan that only gets slower as the table grows.
    supersedes_trace_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True, index=True)
    contributor: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    outcome: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # Hub-computed / read-only fields ------------------------------------
    trust: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    # BigInteger, not Integer: these are unbounded monotonic counters --
    # never decremented, never reset -- incremented on every matching
    # search_traces/get_trace/amend_trace call over the life of a
    # long-lived, frequently-retrieved trace. A plain 32-bit Integer caps
    # out at ~2.1 billion; Postgres raises "integer out of range" on the
    # UPDATE ... SET x = x + 1 the moment a counter would cross that
    # ceiling, turning an otherwise-ordinary read into an unhandled 500 for
    # every future call touching that row. BigInteger costs nothing extra
    # in practice for a counter column.
    retrievals: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    depth: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # Idempotency for contribute_trace: an MCP client that times out waiting
    # for a response has no way to tell "the write never happened" from
    # "the write happened but the response was lost", so it must be safe to
    # retry with the same key. NULL (the default, no key supplied) never
    # conflicts with anything -- Postgres unique constraints treat every
    # NULL as distinct from every other NULL -- so unkeyed contribute_trace
    # calls are unaffected. request_hash lets a retry with the SAME key but
    # a DIFFERENT payload be detected as a caller bug instead of silently
    # returning the wrong (stale) trace. See hub/crud.py:contribute_trace.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Governance / abuse-control fields, not part of the wire Trace object
    quarantined: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    quarantine_reason: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    # --- CommonTrace Knowledge Base membership ----------------------------
    #
    # This is the ONLY field that can make a trace visible outside its owning
    # org, and it is false by default and never set implicitly. Every other
    # read path in hub/crud.py stays unconditionally scoped to the caller's
    # own org_id regardless of this flag; the Knowledge Base is a separate,
    # additive query surface (crud.commons_overlap, crud.commons_search),
    # not a relaxation of the existing one. hub/tests/test_tenant_isolation.py
    # passes unchanged.
    #
    # In production this is set ONLY by hub/manage.py:commons_seed, on rows
    # owned by the operator's own org -- never by a customer action, and
    # never on a customer's own trace. `commons_source == "seed"` (below) is
    # the field every Knowledge Base query actually filters on; this flag
    # alone is defense-in-depth, not the boundary itself.
    shared_with_commons: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    shared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Free-text justification recorded at seed time for why this entry
    # belongs in the Knowledge Base -- substrate knowledge (STRATEGY.md §4),
    # never any customer's business logic. Stored so a reviewer can audit
    # what the operator believed it was publishing and why.
    shared_rationale: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    # MinHash signature of this trace's matchable text, computed once at
    # share time by commontrace.overlap. Precomputed rather than derived per
    # query because commons_overlap compares every submitted failure against
    # every commons trace -- recomputing signatures on each request would
    # make the query cost grow with corpus size for no reason. NULL for any
    # trace that is not in the commons.
    commons_signature: Mapped[list[int] | None] = mapped_column(JSONB, nullable=True)

    # --- Knowledge Base content quality ------------------------------------
    #
    # How many times this Knowledge Base entry has actually matched another
    # org's recurring failure. hub/manage.py:kb_stats uses this to answer
    # "is the Knowledge Base actually earning its query traffic" -- an entry
    # with zero hits after real query volume is filler, not knowledge,
    # however confident the operator was when writing it.
    #
    # Deliberately a counter and not a join table of who-matched-what: the
    # aggregate is what a content-quality report needs, while a per-match
    # log of "org X's failure resembled entry Y" is a far more sensitive
    # artifact for a marginal gain. Incremented with the same atomic
    # in-database UPDATE the retrievals counter uses. BigInteger for the
    # same overflow reason as retrievals/depth above.
    commons_hits: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # --- Knowledge Base entry standing -------------------------------------
    #
    # See hub/commons.py's "Entry standing" section for the model these four
    # columns serve and for why nothing here ever removes an entry
    # automatically. In short: growing a curated corpus and maintaining one
    # are different problems, and only the first was built.
    #
    # Total votes cast on this entry, denormalized alongside `trust` by the
    # same atomic UPDATE in hub/crud.py:vote_trace. `trust` alone cannot
    # distinguish "every fleet that tried this said it failed" (trust 0.0,
    # 12 votes) from "one fleet downvoted it" (trust 0.0, 1 vote), and the
    # whole standing model turns on that difference.
    #
    # A counter rather than a COUNT(*) join on `votes` because both
    # Knowledge Base queries scan up to commons.max_corpus_scan() rows per
    # call and neither can afford a per-row aggregate -- the same reason
    # commons_hits above is a counter. Written as an assignment, not an
    # increment: vote_trace UPSERTs, so an org changing its vote must not
    # add to the total, and the tally it already computes is authoritative.
    commons_votes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # When this entry stops being trustworthy on its own schedule. NULL --
    # the default, and correct for most entries -- means "does not expire":
    # substrate knowledge like idempotency keys on webhook handlers is not
    # pinned to a version and never becomes stale. Set it for knowledge that
    # IS pinned ("React 19 hydrates Date differently than 18"), from the
    # seed file's `review_after` field, and the entry surfaces in
    # `hub/manage.py kb-review` once the date passes.
    #
    # DateTime, unlike the free-text `review_after` column further up: that
    # one is a protocol field carrying whatever the Trace author wrote
    # ("after the next release"), which is fine for a human reading one
    # trace and useless for a query that has to decide what is due.
    commons_review_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Set by `hub/manage.py kb-retract` when an operator pulls an entry.
    # NOT a delete: the row, its votes, and its hit history stay, because
    # "this entry was published, served N failures, and was then withdrawn
    # for reason R" is exactly what an operator needs to keep and exactly
    # what a DELETE destroys. Restorable via `kb-restore`.
    #
    # A retracted entry is invisible to every Knowledge Base read path
    # (commons_overlap, commons_search, and vote_trace's Knowledge Base
    # branch) -- see hub/crud.py:commons_visible, which is the single
    # place that filter is expressed so a fourth read path cannot forget it.
    commons_retracted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    commons_retraction_reason: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    # Where this row came from. An empty Knowledge Base returns 0% coverage
    # for everyone, which is a cold start, not a finding -- so an operator
    # seeds it with authored substrate knowledge to make the first query
    # meaningful. Seeded content must stay DISTINGUISHABLE, or "how many
    # orgs actually consult it" (hub/manage.py:kb_stats' adoption count)
    # silently counts queries against the operator's own seeding and the
    # number stops meaning anything. "org" | "seed" -- "seed" is the only
    # value any Knowledge Base query (commons_overlap, commons_search,
    # vote_trace) will ever match against; see hub/commons.py's module
    # docstring.
    commons_source: Mapped[str] = mapped_column(String(16), default="org", nullable=False)

    # Full-text search vector, maintained by Postgres itself (GENERATED ...
    # STORED) so it can never drift from the columns it summarizes -- there
    # is no application-side "remember to reindex on update" step to forget.
    #
    # This replaces a `title ILIKE '%q%' OR context_text ILIKE '%q%' OR ...`
    # scan. That form cannot use any index (a leading wildcard defeats
    # B-tree prefix matching), so every search was a full sequential scan of
    # the org's traces; with the GIN index below, matching is index-backed.
    # See hub/crud.py:search_traces for the semantic difference this
    # introduces (word/stem matching instead of raw substring matching).
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            f"to_tsvector('{TEXT_SEARCH_CONFIG}', "
            "title || ' ' || context_text || ' ' || solution_text)",
            persisted=True,
        ),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_traces_org_quarantined", "org_id", "quarantined"),
        Index("ix_traces_tags_gin", "tags", postgresql_using="gin"),
        Index("ix_traces_search_vector_gin", "search_vector", postgresql_using="gin"),
        # search_traces orders by created_at DESC within an org; without this
        # the ordering step sorts the whole org partition on every query.
        Index("ix_traces_org_created_at", "org_id", "created_at"),
        # agents_under_management is COUNT(DISTINCT agent_id) over one org
        # within a trailing time window, and it runs on the write path
        # (_reserve_agent_slot) on every contribute_trace, not just in
        # reporting. Leading org_id + created_at serves the equality-then-
        # range predicate, and carrying agent_id as the third column makes
        # the distinct step index-only rather than a heap fetch per row --
        # which matters precisely for the largest fleets, the ones whose
        # agent count this exists to measure.
        Index("ix_traces_org_created_agent", "org_id", "created_at", "agent_id"),
        UniqueConstraint("org_id", "idempotency_key", name="uq_traces_org_idempotency_key"),
        # commons_overlap scans the commons corpus -- traces shared, not
        # quarantined, not retracted -- across ALL orgs. Partial index: the
        # commons is expected to be a small minority of rows for a long
        # time, so indexing only the shared ones keeps it tiny and keeps the
        # scan off the main table.
        #
        # The predicate tracks hub/crud.py:commons_visible exactly. A
        # partial index only serves a query whose WHERE clause implies the
        # index's own, so letting the two drift does not produce wrong
        # answers -- it silently stops using the index and turns every
        # Knowledge Base query back into a full scan of `traces`.
        Index(
            "ix_traces_commons",
            "shared_with_commons",
            postgresql_where=text(
                "shared_with_commons AND NOT quarantined AND commons_retracted_at IS NULL"
            ),
        ),
    )


VALID_SUBMISSION_STATUSES = ("pending", "approved", "rejected")


class KnowledgeBaseSubmission(Base):
    """A customer-proposed CommonTrace Knowledge Base entry, pending
    operator review. This is the only path by which a customer can ever
    cause new content to enter the Knowledge Base -- and even then, only
    indirectly. Modeled on Stack Overflow / a wiki edit queue rather than
    the retired org-to-org `share_trace`: an org writes up a generalized
    substrate lesson (not a live pointer into its own private trace
    history), and hub/manage.py review-submission is the one deliberate
    operator action that can turn an *approved* row into a new `Trace`
    with `commons_source='seed'`.

    A submission is never itself queryable by commons_overlap/
    commons_search: it carries no `commons_signature`, lives in a separate
    table from `Trace` entirely, and a pending or rejected submission is
    never even read by hub/commons.py's matching code. There is no window
    in which unreviewed content is live.

    WHY REVIEW, NOT JUST OPT-IN. STRATEGY.md §3's adverse-selection
    argument holds against ANY credit-for-contributing design where
    contribution alone earns the credit: an org keeps its genuinely
    valuable lessons and contributes generic filler to collect the reward.
    Gating the credit on operator ACCEPTANCE instead changes the incentive
    from "contribute anything" to "write something worth publishing" --
    filler gets rejected and earns nothing, so it stops being a viable
    strategy for extracting query allowance. This does not make the
    underlying tension disappear (an org still has no reason to hand over
    its most differentiated knowledge), it only means what accumulates is
    self-selected for being non-competitive, exactly like a Stack Overflow
    answer or a Wikipedia edit.
    """

    __tablename__ = "kb_submissions"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    context_text: Mapped[str] = mapped_column(Text, nullable=False)
    solution_text: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(128)), default=list, nullable=False)
    agent_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # Caller-supplied justification for why this is substrate knowledge,
    # not business logic -- the same judgment hub/manage.py:commons_seed
    # already requires of the operator, asked of the proposer up front so
    # an operator working through a review queue has a starting point
    # instead of raw text alone.
    rationale: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    rejection_reason: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    # Set only on approval. Not a ForeignKey, for the same reason
    # Trace.supersedes_trace_id is not one: a later purge-trace on the
    # resulting entry must not be blocked by this row referencing it.
    resulting_trace_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    # What review-submission actually granted. Recorded on the submission
    # itself (not just added to the org's running total) so an audit of
    # "why does this org have N bonus queries" is answerable from this
    # table alone, without reconstructing it from AuditLogEntry summaries.
    credit_awarded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Idempotency, identical in shape and purpose to Trace's -- a client
    # that timed out waiting for a submit_kb_entry response must be able to
    # retry with the same key rather than double-submitting.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')", name="ck_kb_submissions_status"
        ),
        Index("ix_kb_submissions_org_created_at", "org_id", "created_at"),
        # The operator review queue lists pending submissions across ALL
        # orgs -- a partial index keyed on the status Postgres will
        # actually be asked to filter on keeps that query cheap regardless
        # of how large the approved/rejected history grows.
        Index("ix_kb_submissions_pending", "status", postgresql_where=text("status = 'pending'")),
        UniqueConstraint("org_id", "idempotency_key", name="uq_kb_submissions_org_idempotency_key"),
    )


# Allowed feedback_tag values, as both the source of truth for the DB CHECK
# constraint below AND for hub/crud.py's application-level validation
# (VALID_VOTE_TYPES likewise for vote_type). Single source of truth so the
# two cannot drift the way hub/tests/test_commons.py's client/server
# signature identity check exists to prevent elsewhere in this codebase --
# without app-level validation matching this exactly, a caller sending a
# tag outside the enum reaches the CHECK constraint only, which fails as an
# uncaught IntegrityError (an opaque HTTP 500) rather than a clean 400.
VALID_VOTE_TYPES = ("up", "down")
VALID_FEEDBACK_TAGS = ("", "outdated", "wrong", "security_concern", "spam")
MAX_FEEDBACK_TEXT_CHARS = 2000


class Vote(Base):
    __tablename__ = "votes"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    trace_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("traces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vote_type: Mapped[str] = mapped_column(String(8), nullable=False)
    feedback_tag: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    # Text, not unbounded in practice: hub/crud.py:vote_trace enforces
    # MAX_FEEDBACK_TEXT_CHARS before this ever reaches the database. The
    # column itself stays Text rather than String(N) so a lowered
    # application-level cap in the future does not require a migration.
    feedback_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        CheckConstraint("vote_type IN ('up', 'down')", name="ck_votes_vote_type"),
        CheckConstraint(
            "feedback_tag IN ('', 'outdated', 'wrong', 'security_concern', 'spam')",
            name="ck_votes_feedback_tag",
        ),
        # One org casts at most one standing vote per trace; a repeat vote
        # updates the existing row instead of accumulating duplicates.
        UniqueConstraint("trace_id", "org_id", name="uq_votes_trace_org"),
    )


class HoldoutObservation(Base):
    """One occasion on which one trace was ELIGIBLE to be injected, which
    arm it landed in, and whether the task succeeded.

    This is the only structure in the Hub that supports a CAUSAL claim.
    `hub/outcomes.py` compares a fleet against its own past, which cannot
    separate this product's contribution from anything else that changed
    in the same window. This compares two arms of the same fleet in the
    same window, differing only by whether the memory was injected -- so
    "what else changed that quarter?" has an answer, and the answer is
    "nothing, by construction".

    STRATEGY.md §11.3 names causally-measured memory as the entire moat
    and §13.2 calls running it "the cheapest falsifier in the document",
    to be run first. Both were true of `commontrace/experiment.py`, which
    works against a local file store. Nothing in the Hub could do it --
    so the falsifier could not be run on the surface where paying
    customers actually are.

    WHY `eligible` IS NOT A COLUMN
    ------------------------------
    commontrace/experiment.py:HoldoutObservation calls `eligible` "the
    crucial field and the easiest thing to get wrong": the comparison is
    only valid across occasions where the memory's activation condition
    matched, and comparing "injected" against "every occasion it never
    matched" reintroduces exactly the confound the holdout removes.

    Here it cannot be gotten wrong, because a row only exists when the
    Hub was asked to decide about that (trace, occasion) pair --
    `hub/crud.py:holdout_assign` is called with traces that already
    matched. Eligibility is the row's existence rather than a flag on it,
    which is one fewer thing a client can report incorrectly.
    """

    __tablename__ = "holdout_observations"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Not a ForeignKey, for the same reason Trace.supersedes_trace_id is
    # not: a trace can be deleted (delete_trace, purge-trace) and a dangling
    # FK would either block that deletion or silently erase the measurement
    # it belongs to. An observation about a since-deleted trace is still a
    # valid data point about the experiment that ran.
    trace_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False, index=True)

    # The task/attempt this decision was made for. Opaque to the Hub: the
    # client's own identifier for one unit of work, and the key the outcome
    # is later reported against.
    occasion_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # Which arm. False means deliberately withheld -- the control.
    injected: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # NULL until the client reports how the occasion went. An observation
    # with no outcome is excluded from the analysis rather than counted as
    # a failure: an agent that crashed before reporting is missing data,
    # and scoring it as a loss would bias the arm that crashed more.
    succeeded: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Recorded so a salt change is detectable after the fact rather than
    # silently mixing two randomizations (see Organization.holdout_salt).
    salt: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # One decision per (trace, occasion) per experiment. Without this a
        # client retrying an assign call would create a second row -- and
        # since assignment is deterministic both rows land in the same arm,
        # so the duplicate would not look wrong, it would just silently
        # double that occasion's weight in the result.
        UniqueConstraint(
            "org_id", "salt", "trace_id", "occasion_id", name="uq_holdout_org_salt_trace_occasion"
        ),
        # record_occasion_outcome updates every row for one occasion.
        Index("ix_holdout_org_occasion", "org_id", "occasion_id"),
        # The analysis reads one org's resolved observations for one salt.
        Index("ix_holdout_org_salt", "org_id", "salt"),
    )


class TraceRelation(Base):
    """Hub-computed relationship edges backing Trace.related. Populated by
    amend_trace (AMENDS + SUPERSEDES); CO_RETRIEVED is not computed yet --
    see hub/README.md "Not implemented" for why."""

    __tablename__ = "trace_relations"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    trace_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("traces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Not a ForeignKey (see hub/manage.py:purge_trace -- a relation row
    # where this trace is the TARGET is not covered by trace_id's own
    # FK/CASCADE, deliberately, so purge_trace can clean it up explicitly
    # instead). Indexed anyway: that same purge path, and crud.py's own
    # relation lookups, filter on it directly.
    related_trace_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False, index=True)
    relationship_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class AuditLogEntry(Base):
    """Append-only record of consequential actions, so a deployment holding
    several organizations' data can answer "who did what, when."

    Deliberately NOT ON DELETE CASCADE from organizations: `org_id` is a
    plain column, not a foreign key. Purging an org must not erase the
    record that the purge happened -- that is precisely the event an audit
    trail exists to retain (and the reason `actor` and `summary` are
    denormalized strings rather than joins to rows that may no longer
    exist).

    Scope, stated plainly so nobody over-reads it: this captures
    *mutating* operations -- writes via the MCP tools and every
    hub/manage.py admin command. It is not a full request log; ordinary
    reads (search_traces/get_trace/list_tags) are not recorded here, since
    logging every read of a knowledge store is high-volume and low-signal.
    Read-side visibility comes from the request logs instead.
    """

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    # Who. "api-key:<key_prefix>" for MCP-tool actions (the non-secret
    # prefix, never the key itself); "operator-cli" for hub/manage.py.
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    # Which org the action was scoped to, when applicable. Not an FK -- see
    # the class docstring.
    org_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True, index=True)

    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    # Short human-readable context. Must never contain secrets or full
    # trace bodies -- see hub/audit.py for what callers are expected to put
    # here.
    summary: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    __table_args__ = (
        Index("ix_audit_log_org_created_at", "org_id", "created_at"),
    )


class UsageCounter(Base):
    """Metered usage, one row per (org, billing period, metric).

    WHY A TABLE AND NOT A COUNTER IN MEMORY. The Hub runs as more than one
    process -- that is the whole point of the container -- and a per-process
    counter multiplies every limit by the replica count. The rate limiter
    already carries that caveat for throttling, where the failure mode is
    only "slightly too permissive for a few seconds". For entitlements the
    failure mode is unbilled usage that scales with how well the service is
    doing, so it has to be shared state.

    WHY A DENORMALIZED PERIOD STRING. `period` is 'YYYY-MM' in UTC, so the
    natural key is exact and index-friendly, and the monthly reset needs no
    job: a new month is simply a row that does not exist yet. Deriving the
    period from `created_at` at query time instead would make every read a
    range scan over an ever-growing table, and would put the month boundary
    at the mercy of the reading session's timezone.

    Increments go through INSERT ... ON CONFLICT DO UPDATE SET n = n + 1
    (hub/crud.py), never read-modify-write: two concurrent queries from the
    same org would otherwise both read n and both write n+1, and the org
    would get a free query every time it ran anything in parallel. That is
    the same lost-update class as the vote race, and it costs money here.
    """

    __tablename__ = "usage_counters"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    n: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        # Named explicitly: the atomic upsert in hub/crud.py targets this
        # constraint by name, and an auto-generated name would break that
        # silently on a schema rebuild.
        UniqueConstraint("org_id", "period", "metric", name="uq_usage_org_period_metric"),
    )
