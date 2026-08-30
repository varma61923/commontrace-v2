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
    op.add_column(
        "traces",
        sa.Column("commons_votes", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "traces", sa.Column("commons_review_after", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "traces", sa.Column("commons_retracted_at", sa.DateTime(timezone=True), nullable=True)
    )
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
    op.drop_index("ix_traces_commons", table_name="traces")
    op.create_index(
        "ix_traces_commons",
        "traces",
        ["shared_with_commons"],
        unique=False,
        postgresql_where=sa.text(
            "shared_with_commons AND NOT quarantined AND commons_retracted_at IS NULL"
        ),
    )


def downgrade() -> None:
    # Rebuilt first: the index below still references commons_retracted_at,
    # so it cannot survive the column drop.
    op.drop_index("ix_traces_commons", table_name="traces")
    op.create_index(
        "ix_traces_commons",
        "traces",
        ["shared_with_commons"],
        unique=False,
        postgresql_where=sa.text("shared_with_commons AND NOT quarantined"),
    )
    op.drop_column("traces", "commons_retraction_reason")
    op.drop_column("traces", "commons_retracted_at")
    op.drop_column("traces", "commons_review_after")
    op.drop_column("traces", "commons_votes")
