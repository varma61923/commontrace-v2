"""Remove webhook destination credentials from historical audit summaries."""
from alembic import op

revision = "ff58bac2d4f6"
down_revision = "ff47a9b1c3e5"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE audit_log SET summary = 'Webhook destination redacted' "
               "WHERE action IN ('webhook.add', 'webhook.disable')")


def downgrade():
    # Credential removal is intentionally irreversible.
    pass
