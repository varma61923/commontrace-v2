"""trace agent_id + agents-under-management index

Adds the agent IDENTITY that `agent_type` was never able to carry:
agent_type is a category ("support"), so a fleet of 25 support agents
shares one value and the number of agents an org runs was not computable
from anything stored. See hub/models.py:Trace.agent_id and hub/plans.py.

Backfill is a server_default of '' applied to every existing row, so this
migration is safe on a live table with no application coordination: old
clients that never send an agent_id keep writing, and their traces are
attributed to the single UNATTRIBUTED sentinel agent per org rather than
being rejected or silently uncounted.

Revision ID: 7c4a1d92e5b8
Revises: 40d3f29cbb24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7c4a1d92e5b8"
down_revision: Union[str, None] = "40d3f29cbb24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # autocommit_block() below unconditionally commits whatever transaction
    # precedes it (Alembic's own documented behavior for that call) -- so
    # add_column, if reached and run every time, is committed for good the
    # instant the block is entered, WHETHER OR NOT the CONCURRENTLY index
    # build after it goes on to succeed. A process killed mid-build (deploy
    # timeout, OOM, an operator's Ctrl-C) then leaves agent_id on `traces`
    # with alembic_version never advanced past this revision, so a plain
    # retry re-enters upgrade() from the top -- and op.add_column is not
    # idempotent, so it would fail with a duplicate-column error before
    # ever reaching the index step. Checked and skipped here so a retry
    # after an interrupted first attempt can still complete on its own.
    inspector = sa.inspect(op.get_bind())
    if "agent_id" not in {c["name"] for c in inspector.get_columns("traces")}:
        # server_default='' rather than nullable=True: every pre-existing
        # row gets a concrete value in one pass, so no COUNT(DISTINCT
        # agent_id) downstream has to special-case NULL. Postgres 11+ adds
        # a non-volatile DEFAULT without rewriting the table, so this stays
        # cheap on a large traces table.
        op.add_column(
            "traces",
            sa.Column("agent_id", sa.String(length=128), nullable=False, server_default=""),
        )
    # CONCURRENTLY, in its own autocommit_block: a plain CREATE INDEX takes
    # a SHARE lock on `traces` for as long as the build takes, which blocks
    # every INSERT/UPDATE/DELETE against it fleet-wide for the duration --
    # on a live table, that means contribute_trace/amend_trace/vote_trace
    # all stall until this one index finishes. CONCURRENTLY avoids that
    # lock at the cost of two table scans instead of one, which is the
    # right trade for a migration applied to a table already taking
    # traffic. It cannot run inside a transaction block at all (a hard
    # Postgres restriction), and hub/alembic/env.py wraps every migration
    # in one by default -- autocommit_block() is Alembic's own supported
    # way to commit the surrounding transaction, run this one statement
    # outside it, and resume, without restructuring env.py's transaction
    # handling for every other (transaction-safe) migration.
    with op.get_context().autocommit_block():
        # DROP ... IF EXISTS first: a CONCURRENTLY build interrupted
        # partway leaves an INVALID index under this exact name in the
        # catalog, not no index at all -- a bare retry of CREATE INDEX
        # (even CONCURRENTLY IF NOT EXISTS, which only checks the name, not
        # validity) would then either fail on "already exists" or silently
        # leave the broken index in place forever. Dropping first, with no
        # error if the previous attempt never got far enough to leave one
        # behind, makes every retry rebuild the index from a clean slate.
        op.drop_index(
            "ix_traces_org_created_agent",
            table_name="traces",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.create_index(
            "ix_traces_org_created_agent",
            "traces",
            ["org_id", "created_at", "agent_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_traces_org_created_agent",
            table_name="traces",
            postgresql_concurrently=True,
            if_exists=True,
        )
    op.drop_column("traces", "agent_id")
