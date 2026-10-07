"""scope executions and operational state by trading account

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("trades") as batch_op:
        batch_op.add_column(
            sa.Column("account_id", sa.String(), server_default="default", nullable=False)
        )
        batch_op.drop_constraint("uq_trades_signal_fingerprint", type_="unique")
        batch_op.drop_constraint("uq_trades_mt5_position_id", type_="unique")
        batch_op.create_unique_constraint(
            "uq_trades_account_fingerprint", ["account_id", "signal_fingerprint"]
        )
        batch_op.create_unique_constraint(
            "uq_trades_account_position", ["account_id", "mt5_position_id"]
        )
        batch_op.create_index("ix_trades_account_id", ["account_id"])

    with op.batch_alter_table("progression_states") as batch_op:
        batch_op.add_column(
            sa.Column("account_id", sa.String(), server_default="default", nullable=False)
        )
        batch_op.create_index("ix_progression_states_account_id", ["account_id"])

    with op.batch_alter_table("audit_events") as batch_op:
        batch_op.add_column(sa.Column("account_id", sa.String(), nullable=True))
        batch_op.create_index("ix_audit_events_account_id", ["account_id"])


def downgrade() -> None:
    with op.batch_alter_table("audit_events") as batch_op:
        batch_op.drop_index("ix_audit_events_account_id")
        batch_op.drop_column("account_id")

    with op.batch_alter_table("progression_states") as batch_op:
        batch_op.drop_index("ix_progression_states_account_id")
        batch_op.drop_column("account_id")

    with op.batch_alter_table("trades") as batch_op:
        batch_op.drop_index("ix_trades_account_id")
        batch_op.drop_constraint("uq_trades_account_position", type_="unique")
        batch_op.drop_constraint("uq_trades_account_fingerprint", type_="unique")
        batch_op.create_unique_constraint("uq_trades_mt5_position_id", ["mt5_position_id"])
        batch_op.create_unique_constraint("uq_trades_signal_fingerprint", ["signal_fingerprint"])
        batch_op.drop_column("account_id")
