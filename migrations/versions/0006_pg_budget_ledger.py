"""PostgreSQL/SQLite 事务预算账本表：作用域锁根、预占结算明细与审计事件。"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "budget_ledgers",
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("token_limit", sa.BigInteger(), nullable=False),
        sa.Column("model_limits", sa.JSON(), nullable=False),
        sa.Column("actual_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("unknown_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("in_flight_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("model_usage", sa.JSON(), nullable=False),
        sa.Column("halted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("halted_reason", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("scope"),
    )

    op.create_table(
        "budget_reservations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("settlement_key", sa.String(length=128), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("estimate", sa.BigInteger(), nullable=False),
        sa.Column("actual", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="reserved", nullable=False),
        sa.Column("over_reserve", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("pid", sa.Integer(), server_default="0", nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column("reserved_at", sa.DateTime(), nullable=False),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["scope"], ["budget_ledgers.scope"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_budget_reservations_scope", "budget_reservations", ["scope"], unique=False)
    op.create_index("ix_budget_reservations_model", "budget_reservations", ["model"], unique=False)
    op.create_index("ix_budget_reservations_status", "budget_reservations", ["status"], unique=False)
    op.create_index(
        "uq_budget_res_scope_idem", "budget_reservations", ["scope", "idempotency_key"], unique=True
    )
    op.create_index(
        "uq_budget_res_scope_settle", "budget_reservations", ["scope", "settlement_key"], unique=True
    )

    op.create_table(
        "budget_ledger_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("reservation_id", sa.String(length=64), nullable=True),
        sa.Column("pid", sa.Integer(), server_default="0", nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["scope"], ["budget_ledgers.scope"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_budget_ledger_events_scope", "budget_ledger_events", ["scope"], unique=False)
    op.create_index(
        "uq_budget_event_scope_idem", "budget_ledger_events", ["scope", "idempotency_key"], unique=True
    )


def downgrade():
    op.drop_index("uq_budget_event_scope_idem", table_name="budget_ledger_events")
    op.drop_index("ix_budget_ledger_events_scope", table_name="budget_ledger_events")
    op.drop_table("budget_ledger_events")

    op.drop_index("uq_budget_res_scope_settle", table_name="budget_reservations")
    op.drop_index("uq_budget_res_scope_idem", table_name="budget_reservations")
    op.drop_index("ix_budget_reservations_status", table_name="budget_reservations")
    op.drop_index("ix_budget_reservations_model", table_name="budget_reservations")
    op.drop_index("ix_budget_reservations_scope", table_name="budget_reservations")
    op.drop_table("budget_reservations")

    op.drop_table("budget_ledgers")
