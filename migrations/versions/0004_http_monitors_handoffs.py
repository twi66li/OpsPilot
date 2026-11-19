"""持久化网站巡检配置和问题交接快照。"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "website_monitors",
        sa.Column("site_id", sa.String(64), sa.ForeignKey("website_profiles.id"), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("next_check_at", sa.DateTime(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_index("ix_website_monitors_next_check_at", "website_monitors", ["next_check_at"])
    op.create_table(
        "case_handoffs",
        sa.Column("case_id", sa.String(64), sa.ForeignKey("support_cases.session_id"), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("recipient_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_index("ix_case_handoffs_recipient_id", "case_handoffs", ["recipient_id"])


def downgrade():
    op.drop_index("ix_case_handoffs_recipient_id", table_name="case_handoffs")
    op.drop_table("case_handoffs")
    op.drop_index("ix_website_monitors_next_check_at", table_name="website_monitors")
    op.drop_table("website_monitors")
