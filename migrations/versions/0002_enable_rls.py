"""Enable row level security on every table (Postgres/Supabase only).

The backend connects as the table owner, which bypasses RLS, so the API is unaffected.
With RLS on and no policies, Supabase's Data API roles (anon, authenticated) can't read or write
these tables. The frontend never talks to Supabase directly, so no policies are needed.

Revision ID: 0002_enable_rls
Revises: ade886fd8376
"""
from alembic import op

revision = "0002_enable_rls"
down_revision = "ade886fd8376"
branch_labels = None
depends_on = None

TABLES = ["dispute_runs", "upload_sessions", "evidence_items", "merchant_edits", "webhook_events",
          "metric_events", "link_tokens", "idempotency_keys", "alembic_version"]


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for t in TABLES:
        op.execute(f"alter table public.{t} enable row level security")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for t in TABLES:
        op.execute(f"alter table public.{t} disable row level security")
