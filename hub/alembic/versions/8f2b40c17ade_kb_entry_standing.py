"""Knowledge Base entry standing: votes, freshness horizon, retraction"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "8f2b40c17ade"
down_revision: Union[str, None] = "3c3adaa53a71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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

    op.execute(
        """
        UPDATE traces AS t
           SET commons_votes = v.n
          FROM (SELECT trace_id, COUNT(*) AS n FROM votes GROUP BY trace_id) AS v
         WHERE v.trace_id = t.id
        """
    )

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
