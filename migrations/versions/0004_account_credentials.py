"""encrypted account credentials and one-time setup links

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_credentials",
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("login", sa.Integer(), nullable=False),
        sa.Column("server", sa.String(), nullable=False),
        sa.Column("encrypted_password", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("account_id", name="pk_account_credentials"),
    )
    op.create_table(
        "account_setup_tokens",
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("token_hash", name="pk_account_setup_tokens"),
    )
    with op.batch_alter_table("account_setup_tokens") as batch_op:
        batch_op.create_index("ix_account_setup_tokens_account_id", ["account_id"])
        batch_op.create_index("ix_account_setup_tokens_expires_at", ["expires_at"])


def downgrade() -> None:
    with op.batch_alter_table("account_setup_tokens") as batch_op:
        batch_op.drop_index("ix_account_setup_tokens_expires_at")
        batch_op.drop_index("ix_account_setup_tokens_account_id")
    op.drop_table("account_setup_tokens")
    op.drop_table("account_credentials")
