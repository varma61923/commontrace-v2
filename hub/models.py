"""SQLAlchemy 2.0 ORM models for the Hub's Postgres store."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

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
    plan: Mapped[str] = mapped_column(String(32), default="free", nullable=False)
    data_region: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bonus_commons_queries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    commons_auto_contribute: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    trace_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    share_generation: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    holdout_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    holdout_salt: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    holdout_prereg: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    harm_policy: Mapped[str] = mapped_column(
        String(16), default="inform", server_default="inform", nullable=False
    )

    deletion_token_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    deletion_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deletion_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)

    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="organization", cascade="all, delete-orphan")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    key_hmac: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), nullable=False, server_default="{read,write,admin}", default=list
    )

    organization: Mapped[Organization] = relationship(back_populates="api_keys")


class Trace(Base):
    __tablename__ = "traces"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    context_text: Mapped[str] = mapped_column(Text, nullable=False)
    solution_text: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(128)), default=list, nullable=False)
    subject_ids: Mapped[list[str]] = mapped_column(ARRAY(String(256)), default=list, nullable=False)
    agent_type: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    profile: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    extensions: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    watch_condition: Mapped[str] = mapped_column(Text, default="", nullable=False)
    review_after: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    supersedes_trace_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True, index=True)
    contributor: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    outcome: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    superseded_by_trace_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)

    trust: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    retrievals: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    depth: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    quarantined: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    quarantine_reason: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    shared_with_commons: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    shared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shared_rationale: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    commons_signature: Mapped[list[int] | None] = mapped_column(JSONB, nullable=True)

    commons_hits: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    commons_votes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    commons_review_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    commons_retracted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    commons_retraction_reason: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    commons_source: Mapped[str] = mapped_column(String(16), default="org", nullable=False)

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
        Index("ix_traces_subject_ids_gin", "subject_ids", postgresql_using="gin"),
        Index("ix_traces_search_vector_gin", "search_vector", postgresql_using="gin"),
        Index("ix_traces_org_created_at", "org_id", "created_at"),
        Index("ix_traces_org_created_agent", "org_id", "created_at", "agent_id"),
        UniqueConstraint("org_id", "idempotency_key", name="uq_traces_org_idempotency_key"),
        Index(
            "ix_traces_commons",
            "shared_with_commons",
            postgresql_where=text(
                "shared_with_commons AND NOT quarantined AND commons_retracted_at IS NULL"
            ),
        ),
        Index(
            "ix_traces_org_live",
            "org_id",
            postgresql_where=text("superseded_at IS NULL"),
        ),
    )


VALID_SUBMISSION_STATUSES = ("pending", "approved", "rejected")


class KnowledgeBaseSubmission(Base):
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
    rationale: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    rejection_reason: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    resulting_trace_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    credit_awarded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')", name="ck_kb_submissions_status"
        ),
        Index("ix_kb_submissions_org_created_at", "org_id", "created_at"),
        Index("ix_kb_submissions_pending", "status", postgresql_where=text("status = 'pending'")),
        UniqueConstraint("org_id", "idempotency_key", name="uq_kb_submissions_org_idempotency_key"),
    )


class CommonsCorpusState(Base):
    """One row: a version number for the Knowledge Base's matchable corpus."""

    __tablename__ = "commons_corpus_state"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


COMMONS_CORPUS_COLUMNS = (
    "shared_with_commons",
    "commons_source",
    "quarantined",
    "commons_retracted_at",
    "superseded_at",
    "commons_signature",
    "org_id",
    "agent_type",
    "created_at",
)

_WAS_OR_IS_COMMONS = (
    "(OLD.shared_with_commons OR OLD.commons_source = 'seed' "
    "OR NEW.shared_with_commons OR NEW.commons_source = 'seed')"
)

COMMONS_CORPUS_TRIGGER_DDL = (
    """
    CREATE OR REPLACE FUNCTION commons_corpus_bump() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    BEGIN
        INSERT INTO commons_corpus_state (id, version, changed_at)
        VALUES (1, 1, clock_timestamp())
        ON CONFLICT (id) DO UPDATE
        SET version = commons_corpus_state.version + 1,
            changed_at = clock_timestamp();
        RETURN NULL;
    END
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION commons_corpus_key(OUT version bigint, OUT changed_at timestamptz)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        SELECT version, changed_at FROM commons_corpus_state WHERE id = 1
    $$
    """,
    """
    CREATE TRIGGER commons_corpus_bump_insert
    AFTER INSERT ON traces FOR EACH ROW
    WHEN (NEW.shared_with_commons OR NEW.commons_source = 'seed')
    EXECUTE FUNCTION commons_corpus_bump()
    """,
    """
    CREATE TRIGGER commons_corpus_bump_delete
    AFTER DELETE ON traces FOR EACH ROW
    WHEN (OLD.shared_with_commons OR OLD.commons_source = 'seed')
    EXECUTE FUNCTION commons_corpus_bump()
    """,
    f"""
    CREATE TRIGGER commons_corpus_bump_update
    AFTER UPDATE OF {", ".join(COMMONS_CORPUS_COLUMNS)} ON traces FOR EACH ROW
    WHEN ({_WAS_OR_IS_COMMONS} AND
          ({", ".join("OLD." + c for c in COMMONS_CORPUS_COLUMNS)})
          IS DISTINCT FROM
          ({", ".join("NEW." + c for c in COMMONS_CORPUS_COLUMNS)}))
    EXECUTE FUNCTION commons_corpus_bump()
    """,
)

for _statement in COMMONS_CORPUS_TRIGGER_DDL:
    event.listen(Trace.__table__, "after_create", DDL(_statement))

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
    feedback_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        CheckConstraint("vote_type IN ('up', 'down')", name="ck_votes_vote_type"),
        CheckConstraint(
            "feedback_tag IN ('', 'outdated', 'wrong', 'security_concern', 'spam')",
            name="ck_votes_feedback_tag",
        ),
        UniqueConstraint("trace_id", "org_id", name="uq_votes_trace_org"),
    )


class HoldoutObservation(Base):
    __tablename__ = "holdout_observations"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trace_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False, index=True)

    occasion_id: Mapped[str] = mapped_column(String(128), nullable=False)

    injected: Mapped[bool] = mapped_column(Boolean, nullable=False)

    succeeded: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    salt: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    trace_revision: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "org_id", "salt", "trace_id", "occasion_id", name="uq_holdout_org_salt_trace_occasion"
        ),
        Index("ix_holdout_org_occasion", "org_id", "occasion_id"),
        Index("ix_holdout_org_salt", "org_id", "salt"),
    )


class TraceRelation(Base):
    __tablename__ = "trace_relations"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    trace_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("traces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    related_trace_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False, index=True)
    relationship_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class AuditLogEntry(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    org_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True, index=True)

    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    summary: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    __table_args__ = (
        Index("ix_audit_log_org_created_at", "org_id", "created_at"),
    )


class UsageCounter(Base):
    """Metered usage, one row per (org, billing period, metric)."""

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
        UniqueConstraint("org_id", "period", "metric", name="uq_usage_org_period_metric"),
    )


class RetentionPolicy(Base):
    """How long one org keeps one (object type, status), in days."""

    __tablename__ = "retention_policies"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="any", nullable=False)
    max_age_days: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "org_id", "object_type", "status", name="uq_retention_org_type_status"
        ),
    )


class LegalHold(Base):
    """A freeze that outranks every retention policy."""

    __tablename__ = "legal_holds"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    object_type: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    placed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    release_reason: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    __table_args__ = (
        Index(
            "ix_legal_holds_active",
            "org_id", "object_type",
            postgresql_where=text("released_at IS NULL"),
        ),
    )


class WebhookEndpoint(Base):
    """Where one org wants to be told what happened here."""

    __tablename__ = "webhook_endpoints"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    events: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), default=list, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class WebhookDelivery(Base):
    """One queued attempt to tell someone one thing."""

    __tablename__ = "webhook_deliveries"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    endpoint_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "ix_webhook_deliveries_due", "status", "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)

    issuer: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    external_subject: Mapped[str] = mapped_column(String(255), default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("org_id", "email", name="uq_users_org_email"),
        Index(
            "ix_users_issuer_subject", "issuer", "external_subject", unique=True,
            postgresql_where=text("external_subject != ''"),
        ),
    )


class ScimGroup(Base):
    __tablename__ = "scim_groups"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("org_id", "display_name", name="uq_scim_groups_org_display_name"),
    )


class ScimGroupMembership(Base):
    __tablename__ = "scim_group_memberships"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    group_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("scim_groups.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_scim_group_memberships"),
    )


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    author_user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    body: Mapped[str] = mapped_column(String(4000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        Index("ix_comments_target", "org_id", "target_type", "target_id"),
    )


class Assignment(Base):
    """Who currently owns following up on one target (a trace, today)."""

    __tablename__ = "assignments"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    assignee_user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    assigned_by_user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "org_id", "target_type", "target_id", name="uq_assignments_target",
        ),
    )


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_notifications_user_unread", "org_id", "user_id", "read_at"),
    )


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    comparator: Mapped[str] = mapped_column(String(8), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), default="", nullable=False)

    __table_args__ = (
        Index("ix_alert_rules_org_enabled", "org_id", "enabled"),
    )


class ProcessedWebhookEvent(Base):
    """One row per Stripe event id `hub/billing.py` has already applied."""

    __tablename__ = "processed_webhook_events"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)


class Connector(Base):
    __tablename__ = "connectors"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    secret: Mapped[str] = mapped_column(String(2000), nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConnectorDelivery(Base):
    """The replay ledger: one row per vendor delivery a connector has handled."""

    __tablename__ = "connector_deliveries"
    __table_args__ = (
        UniqueConstraint("connector_id", "delivery_id", "dry_run", name="uq_connector_delivery"),
    )

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    connector_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("connectors.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    delivery_id: Mapped[str] = mapped_column(String(255), nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    outcome: Mapped[str] = mapped_column(String(500), default="", nullable=False)


class PendingOutcome(Base):
    """A candidate success waiting out its window."""

    __tablename__ = "connector_pending_outcomes"
    __table_args__ = (
        UniqueConstraint("connector_id", "occasion_id", name="uq_connector_pending_occasion"),
        Index("ix_connector_pending_connector_ref", "connector_id", "ref"),
    )

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    connector_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("connectors.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    occasion_id: Mapped[str] = mapped_column(String(255), nullable=False)
    ref: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    mature_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
