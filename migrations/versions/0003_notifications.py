"""Notifications: one row per dispute nudge (dispute, kind, channel), RLS on like the other tables.

Revision ID: 0003_notifications
Revises: 0002_enable_rls
"""
import sqlalchemy as sa
from alembic import op

revision = "0003_notifications"
down_revision = "0002_enable_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("dispute_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("recipient", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dispute_id", "kind", "channel", name="uq_notification_once"),
    )
    op.create_index("ix_notifications_created", "notifications", ["created_at"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("alter table public.notifications enable row level security")


def downgrade() -> None:
    op.drop_index("ix_notifications_created", "notifications")
    op.drop_table("notifications")
