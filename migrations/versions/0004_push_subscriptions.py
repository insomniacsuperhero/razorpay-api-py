"""Web Push subscriptions for the installed app.

Revision ID: 0004_push_subscriptions
Revises: 0003_notifications
"""
import sqlalchemy as sa
from alembic import op

revision = "0004_push_subscriptions"
down_revision = "0003_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("merchant_id", sa.Text(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False, unique=True),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("alter table public.push_subscriptions enable row level security")


def downgrade() -> None:
    op.drop_table("push_subscriptions")
