"""search vector, audit log, key expiry"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a9237315f409'
down_revision: Union[str, None] = 'b1e835b3302d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('audit_log',
    sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor', sa.String(length=128), nullable=False),
    sa.Column('org_id', sa.UUID(as_uuid=False), nullable=True),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('target_type', sa.String(length=32), nullable=False),
    sa.Column('target_id', sa.String(length=64), nullable=False),
    sa.Column('summary', sa.String(length=500), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_log_action'), 'audit_log', ['action'], unique=False)
    op.create_index('ix_audit_log_org_created_at', 'audit_log', ['org_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_audit_log_org_id'), 'audit_log', ['org_id'], unique=False)
    op.add_column('api_keys', sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        'traces',
        sa.Column(
            'search_vector',
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', title || ' ' || context_text || ' ' || solution_text)",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    op.create_index('ix_traces_org_created_at', 'traces', ['org_id', 'created_at'], unique=False)
    op.create_index('ix_traces_search_vector_gin', 'traces', ['search_vector'], unique=False, postgresql_using='gin')


def downgrade() -> None:
    op.drop_index('ix_traces_search_vector_gin', table_name='traces', postgresql_using='gin')
    op.drop_index('ix_traces_org_created_at', table_name='traces')
    op.drop_column('traces', 'search_vector')
    op.drop_column('api_keys', 'expires_at')
    op.drop_index(op.f('ix_audit_log_org_id'), table_name='audit_log')
    op.drop_index('ix_audit_log_org_created_at', table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_action'), table_name='audit_log')
    op.drop_table('audit_log')
