"""后台任务（调查与入库）租约领取、心跳续约与幂等键约束。"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    # ---- agent_runs 租约与幂等字段 ----
    op.add_column("agent_runs", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    op.add_column("agent_runs", sa.Column("lease_owner", sa.String(length=128), nullable=True))
    op.add_column("agent_runs", sa.Column("lease_token", sa.String(length=64), nullable=True))
    op.add_column("agent_runs", sa.Column("attempt", sa.Integer(), server_default="0", nullable=False))
    op.add_column("agent_runs", sa.Column("heartbeat_at", sa.DateTime(), nullable=True))
    op.add_column("agent_runs", sa.Column("lease_expires_at", sa.DateTime(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE agent_runs SET idempotency_key = 'run:' || session_id || ':' || request_id "
            "WHERE request_id IS NOT NULL AND idempotency_key IS NULL"
        )
    )
    op.create_index("uq_agent_runs_idempotency_key", "agent_runs", ["tenant_id", "idempotency_key"], unique=True)
    op.create_index("ix_agent_runs_lease_claim", "agent_runs", ["status", "lease_expires_at", "created_at"])

    # ---- document_versions 租约与幂等字段 ----
    op.add_column("document_versions", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    op.add_column("document_versions", sa.Column("lease_owner", sa.String(length=128), nullable=True))
    op.add_column("document_versions", sa.Column("lease_token", sa.String(length=64), nullable=True))
    op.add_column("document_versions", sa.Column("attempt", sa.Integer(), server_default="0", nullable=False))
    op.add_column("document_versions", sa.Column("heartbeat_at", sa.DateTime(), nullable=True))
    op.add_column("document_versions", sa.Column("lease_expires_at", sa.DateTime(), nullable=True))
    op.add_column("document_versions", sa.Column("updated_at", sa.DateTime(), nullable=True))
    op.execute(sa.text("UPDATE document_versions SET updated_at = created_at WHERE updated_at IS NULL"))
    op.create_index(
        "uq_document_versions_idempotency_key",
        "document_versions",
        ["tenant_id", "idempotency_key"],
        unique=True,
    )
    op.create_index("ix_document_versions_lease_claim", "document_versions", ["status", "lease_expires_at", "created_at"])

    # ---- messages 幂等键 ----
    op.add_column("messages", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    op.create_index("uq_messages_idempotency_key", "messages", ["tenant_id", "idempotency_key"], unique=True)

    # ---- approval_requests 会话级挂起审批单唯一约束 ----
    op.create_index(
        "uq_approval_pending_session",
        "approval_requests",
        ["tenant_id", "session_id"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade():
    op.drop_index("uq_approval_pending_session", table_name="approval_requests")

    op.drop_index("uq_messages_idempotency_key", table_name="messages")
    op.drop_column("messages", "idempotency_key")

    op.drop_index("ix_document_versions_lease_claim", table_name="document_versions")
    op.drop_index("uq_document_versions_idempotency_key", table_name="document_versions")
    op.drop_column("document_versions", "updated_at")
    op.drop_column("document_versions", "lease_expires_at")
    op.drop_column("document_versions", "heartbeat_at")
    op.drop_column("document_versions", "attempt")
    op.drop_column("document_versions", "lease_token")
    op.drop_column("document_versions", "lease_owner")
    op.drop_column("document_versions", "idempotency_key")

    op.drop_index("ix_agent_runs_lease_claim", table_name="agent_runs")
    op.drop_index("uq_agent_runs_idempotency_key", table_name="agent_runs")
    op.drop_column("agent_runs", "lease_expires_at")
    op.drop_column("agent_runs", "heartbeat_at")
    op.drop_column("agent_runs", "attempt")
    op.drop_column("agent_runs", "lease_token")
    op.drop_column("agent_runs", "lease_owner")
    op.drop_column("agent_runs", "idempotency_key")
