"""Knowledge Base corpus version, maintained by triggers

Adds `commons_corpus_state` (one row) and triggers on `traces` that bump
it whenever a change could alter what the Knowledge Base matcher sees. It
lets each Hub process keep the corpus's signature matrix in memory and
reload it only when it actually changed (hub/commons_cache.py), instead of
reloading and decoding every row on every commons_overlap/commons_search
call -- which, measured at 20,000 entries, was ~1.4s of a ~1.5s query.

The statements are copied here rather than imported from hub/models.py,
because a migration is a snapshot of one moment and must not change when
the model does later. hub/tests/test_commons_cache.py compares the two
copies, so they cannot drift silently.

No backfill: the version starts at no row, which the cache reads as "not
yet built", and the first query builds it.

Revision ID: c3e8a1f05b92
Revises: a1c7e4f93d2b
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3e8a1f05b92"
down_revision: Union[str, None] = "a1c7e4f93d2b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TRIGGER_DDL = (
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
    """
    CREATE TRIGGER commons_corpus_bump_update
    AFTER UPDATE OF
        shared_with_commons,
        commons_source,
        quarantined,
        commons_retracted_at,
        superseded_at,
        commons_signature,
        org_id,
        agent_type,
        created_at
    ON traces FOR EACH ROW
    WHEN ((OLD.shared_with_commons OR OLD.commons_source = 'seed'
           OR NEW.shared_with_commons OR NEW.commons_source = 'seed') AND
          (OLD.shared_with_commons,
           OLD.commons_source,
           OLD.quarantined,
           OLD.commons_retracted_at,
           OLD.superseded_at,
           OLD.commons_signature,
           OLD.org_id,
           OLD.agent_type,
           OLD.created_at)
          IS DISTINCT FROM
          (NEW.shared_with_commons,
           NEW.commons_source,
           NEW.quarantined,
           NEW.commons_retracted_at,
           NEW.superseded_at,
           NEW.commons_signature,
           NEW.org_id,
           NEW.agent_type,
           NEW.created_at))
    EXECUTE FUNCTION commons_corpus_bump()
    """,
)


def upgrade() -> None:
    op.create_table(
        "commons_corpus_state",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    for statement in TRIGGER_DDL:
        op.execute(statement)


def downgrade() -> None:
    for name in ("commons_corpus_bump_update", "commons_corpus_bump_delete", "commons_corpus_bump_insert"):
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON traces")
    op.execute("DROP FUNCTION IF EXISTS commons_corpus_bump()")
    op.execute("DROP FUNCTION IF EXISTS commons_corpus_key()")
    op.drop_table("commons_corpus_state")
