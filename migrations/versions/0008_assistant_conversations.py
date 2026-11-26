"""普通聊天会话与每日新对话计数。"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chat_sessions", sa.Column("assistant_chat", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("chat_sessions", sa.Column("first_turn_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("chat_sessions", "first_turn_at")
    op.drop_column("chat_sessions", "assistant_chat")
