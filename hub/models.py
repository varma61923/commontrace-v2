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
    # for a key issued without --expires-days). A non-NULL value is enforced
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
    profile: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    extensions: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    watch_condition: Mapped[str] = mapped_column(Text, default="", nullable=False)
    review_after: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    supersedes_trace_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    contributor: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    outcome: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # Hub-computed / read-only fields ------------------------------------
    trust: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    retrievals: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Governance / abuse-control fields, not part of the wire Trace object
    quarantined: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    quarantine_reason: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    # Reserved for a future opt-in cross-org "commons" milestone -- see
    # hub/README.md "Tenant isolation vs. the cross-org commons pitch".
    # Not read or acted on by any query in hub/crud.py today: every read
    # path is unconditionally scoped to the caller's own org_id regardless
    # of this flag's value.
    shared_with_commons: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

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
    )


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


class TraceRelation(Base):
    """Hub-computed relationship edges backing Trace.related. Populated by
    amend_trace (AMENDS + SUPERSEDES); CO_RETRIEVED is not computed yet --
    see hub/README.md "Not implemented" for why."""

    __tablename__ = "trace_relations"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    trace_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("traces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    related_trace_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
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
