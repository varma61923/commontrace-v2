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
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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

    __table_args__ = (
        Index("ix_traces_org_quarantined", "org_id", "quarantined"),
        Index("ix_traces_tags_gin", "tags", postgresql_using="gin"),
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
