"""资料移出检索，保留历史引用。"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents", sa.Column("deleted_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("documents", "deleted_at")
