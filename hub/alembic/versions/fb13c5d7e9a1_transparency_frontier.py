"""Store each tenant log's Merkle frontier so appends are O(log n).

Existing heads keep a NULL frontier; the next append rebuilds it once from the
leaf-hash column and verifies it against the signed head before extending it.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "fb13c5d7e9a1"
down_revision = "fa02b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("transparency_heads", sa.Column("frontier", postgresql.JSONB(), nullable=True))


def downgrade():
    op.drop_column("transparency_heads", "frontier")
