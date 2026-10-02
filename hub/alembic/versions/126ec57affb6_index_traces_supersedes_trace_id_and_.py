"""index traces supersedes_trace_id and trace_relations related_trace_id"""
from typing import Sequence, Union

from alembic import op

revision: str = '126ec57affb6'
down_revision: Union[str, None] = '4e1e9ecbff1f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(op.f('ix_trace_relations_related_trace_id'), 'trace_relations', ['related_trace_id'], unique=False)
    op.create_index(op.f('ix_traces_supersedes_trace_id'), 'traces', ['supersedes_trace_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_traces_supersedes_trace_id'), table_name='traces')
    op.drop_index(op.f('ix_trace_relations_related_trace_id'), table_name='trace_relations')
