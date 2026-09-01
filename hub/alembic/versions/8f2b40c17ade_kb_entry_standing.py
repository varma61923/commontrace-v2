"""Knowledge Base entry standing: votes, freshness horizon, retraction

Adds the four columns hub/commons.py's standing model needs
(`commons_votes`, `commons_review_after`, `commons_retracted_at`,
`commons_retraction_reason`) and narrows the partial commons index to
match hub/crud.py:commons_visible, which now also excludes retracted
entries.

`commons_votes` is backfilled from the `votes` table rather than left at 0:
the column is a denormalization of a count that already exists, and
starting every previously-voted-on entry at zero votes would report it as
`unproven` when the corpus already holds the evidence to judge it.

Revision ID: 8f2b40c17ade
Revises: 3c3adaa53a71
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "8f2b40c17ade"
down_revision: Union[str, None] = "3c3adaa53a71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # autocommit_block() below unconditionally commits whatever transaction
    # precedes it (Alembic's own documented behavior), including these four
    # add_column calls and the backfill UPDATE that follows them -- all
    # committed for good the instant the block is entered, whether or not
    # the CONCURRENTLY index rebuild after it goes on to succeed. A process
    # killed mid-build then leaves alembic_version stuck one revision
    # behind with these columns already present, and a plain retry
    # re-enters upgrade() from the top -- where op.add_column is not
    # idempotent and would fail with a duplicate-column error before ever
    # reaching the index step. Each is checked and skipped here so a retry
    # after an interrupted first attempt can still complete on its own; the
    # backfill UPDATE needs no equivalent guard, since re-running the same
    # grouped COUNT(*) over `votes` is naturally idempotent.
    existing_columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("traces")}
    if "commons_votes" not in existing_columns:
        op.add_column(
            "traces",
            sa.Column("commons_votes", sa.BigInteger(), server_default="0", nullable=False),
        )
    if "commons_review_after" not in existing_columns:
        op.add_column(
            "traces", sa.Column("commons_review_after", sa.DateTime(timezone=True), nullable=True)
        )
    if "commons_retracted_at" not in existing_columns:
        op.add_column(
            "traces", sa.Column("commons_retracted_at", sa.DateTime(timezone=True), nullable=True)
        )
    if "commons_retraction_reason" not in existing_columns:
        op.add_column(
            "traces",
            sa.Column(
                "commons_retraction_reason", sa.String(length=200), server_default="", nullable=False
            ),
        )

    # One UPDATE ... FROM over the grouped vote counts, not a correlated
    # subquery per row: this runs against every trace that has ever been
    # voted on, and the correlated form re-scans `votes` once per trace.
    op.execute(
        """
        UPDATE traces AS t
           SET commons_votes = v.n
          FROM (SELECT trace_id, COUNT(*) AS n FROM votes GROUP BY trace_id) AS v
         WHERE v.trace_id = t.id
        """
    )

    # The predicate has to be recreated, not altered -- Postgres has no
    # ALTER INDEX ... SET WHERE. Dropped and rebuilt so it keeps serving
    # the Knowledge Base scans, which now carry the retraction filter too;
    # an index whose predicate is broader than the query's still works,
    # but one the query does not imply is simply never used.
    #
    # Both CONCURRENTLY, in their own autocommit_block: a plain DROP/CREATE
    # INDEX takes a lock that blocks writes against `traces` fleet-wide for
    # as long as the rebuild takes (contribute_trace/amend_trace/vote_trace
    # all stall), and CONCURRENTLY avoids that at the cost of extra table
    # scans -- the right trade on a table already taking traffic. Neither
    # form can run inside a transaction block at all (a hard Postgres
    # restriction), and hub/alembic/env.py wraps every migration in one by
    # default; autocommit_block() is Alembic's own supported way to commit
    # the surrounding transaction, run these two statements outside it,
    # and resume, without restructuring env.py's transaction handling for
    # every other (transaction-safe) migration. The bulk UPDATE above
    # stays inside the normal transaction -- only the index rebuild has
    # this restriction.
    # if_exists=True: a retry after an interrupted attempt may already have
    # dropped this index (or left an INVALID one under the same name from a
    # CONCURRENTLY build that failed partway) -- either way, dropping it
    # first with no error if it is already gone lets create_index below
    # always rebuild from a clean slate. See upgrade()'s docstring above
    # for why add_column needs the identical treatment.
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_traces_commons", table_name="traces", postgresql_concurrently=True, if_exists=True
        )
        op.create_index(
            "ix_traces_commons",
            "traces",
            ["shared_with_commons"],
            unique=False,
            postgresql_where=sa.text(
                "shared_with_commons AND NOT quarantined AND commons_retracted_at IS NULL"
            ),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    # Rebuilt first: the index below still references commons_retracted_at,
    # so it cannot survive the column drop. See upgrade()'s comment for why
    # both are CONCURRENTLY inside an autocommit_block, and why the drop is
    # if_exists=True.
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_traces_commons", table_name="traces", postgresql_concurrently=True, if_exists=True
        )
        op.create_index(
            "ix_traces_commons",
            "traces",
            ["shared_with_commons"],
            unique=False,
            postgresql_where=sa.text("shared_with_commons AND NOT quarantined"),
            postgresql_concurrently=True,
        )
    op.drop_column("traces", "commons_retraction_reason")
    op.drop_column("traces", "commons_retracted_at")
    op.drop_column("traces", "commons_review_after")
    op.drop_column("traces", "commons_votes")
