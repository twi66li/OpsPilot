"""运行事件单调序号与唯一约束：支持数据库行级锁原子追加和无空洞 SSE 游标。"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_runs", sa.Column("event_seq", sa.Integer(), server_default="0", nullable=False))
    op.add_column("agent_run_events", sa.Column("seq", sa.Integer(), server_default="0", nullable=False))

    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, run_id FROM agent_run_events ORDER BY run_id ASC, id ASC")).fetchall()
    counters: dict[str, int] = {}
    for event_id, run_id in rows:
        next_seq = counters.get(run_id, 0) + 1
        counters[run_id] = next_seq
        bind.execute(
            sa.text("UPDATE agent_run_events SET seq = :seq WHERE id = :id"),
            {"seq": next_seq, "id": event_id},
        )
    for run_id, max_seq in counters.items():
        bind.execute(
            sa.text("UPDATE agent_runs SET event_seq = :max_seq WHERE id = :run_id"),
            {"max_seq": max_seq, "run_id": run_id},
        )

    op.create_index("uq_agent_run_events_run_seq", "agent_run_events", ["run_id", "seq"], unique=True)


def downgrade():
    op.drop_index("uq_agent_run_events_run_seq", table_name="agent_run_events")
    op.drop_column("agent_run_events", "seq")
    op.drop_column("agent_runs", "event_seq")
