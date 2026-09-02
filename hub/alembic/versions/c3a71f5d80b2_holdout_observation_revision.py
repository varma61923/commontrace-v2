"""holdout observation records which revision of the trace it measured

The Hub's holdout randomizes TRACES, and `amend_trace` rewrites a trace's
title, context and solution in place. So an observation recorded only
`trace_id` -- a stable id pointing at mutable content -- and an experiment
keyed on it pools occasions treated with different text into one arm, then
reports a single effect for a treatment that is an average of two.

This is the same defect `Organization.holdout_salt` exists to make
detectable, one level down: there the randomization could change silently,
here the thing being randomized could. `commontrace/integrity.py`'s
treatment-stability check reads this column.

Nullable, with no backfill. A revision is what the trace said AT ASSIGNMENT
TIME, and for rows written before this column existed that is unrecoverable
-- the amended content is all that survives. Computing today's digest for
them would assert the treatment was stable on exactly the runs where nobody
can know, which is worse than the honest NULL: the check reports "not
checkable" for those and says so.

Revision ID: c3a71f5d80b2
Revises: b7e4c91d2a08
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3a71f5d80b2"
down_revision: Union[str, None] = "b7e4c91d2a08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Adding a NULLABLE column with no default and no backfill: a catalog-only
    # change in Postgres, so it takes no table rewrite and no long lock even on
    # a holdout table with millions of observations.
    op.add_column(
        "holdout_observations",
        sa.Column("trace_revision", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("holdout_observations", "trace_revision")
