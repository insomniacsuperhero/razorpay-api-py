"""Tables from the Backend Implementation Guide, Appendix B (+ idempotency_keys from section 13.2).
Types work on Postgres (JSONB, UUID, timestamptz) and on SQLite for local tests."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import UniqueConstraint, JSON, BigInteger, DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JsonType = JSON().with_variant(JSONB(), "postgresql")
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")   # SQLite only autoincrements INTEGER


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class DisputeRun(Base):
    __tablename__ = "dispute_runs"
    dispute_id: Mapped[str] = mapped_column(Text, primary_key=True)
    payment_id: Mapped[str] = mapped_column(Text)
    merchant_id: Mapped[str] = mapped_column(Text, default="merchant_demo")
    draft_summary: Mapped[str | None] = mapped_column(Text)
    summary_edited: Mapped[bool] = mapped_column(default=False)
    contested_amount: Mapped[int | None] = mapped_column(BigInteger)
    timer_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    evidence: Mapped[list["EvidenceItem"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    uploads: Mapped[list["UploadSession"]] = relationship(cascade="all, delete-orphan")


class UploadSession(Base):
    __tablename__ = "upload_sessions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("dispute_runs.dispute_id", ondelete="CASCADE"))
    object_key: Mapped[str] = mapped_column(Text, unique=True)
    original_filename: Mapped[str] = mapped_column(Text)
    declared_mime_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32))          # pending | uploaded | expired
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvidenceItem(Base):
    __tablename__ = "evidence_items"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("dispute_runs.dispute_id", ondelete="CASCADE"))
    upload_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("upload_sessions.id"))
    original_filename: Mapped[str] = mapped_column(Text, default="")
    original_object_key: Mapped[str] = mapped_column(Text)
    normalized_object_key: Mapped[str | None] = mapped_column(Text)
    detected_mime_type: Mapped[str | None] = mapped_column(Text)
    document_type: Mapped[str | None] = mapped_column(Text)      # plain label, e.g. "Courier tracking screenshot"
    # queued | extracting | analysing | validating | uploading | ready | failed
    status: Mapped[str] = mapped_column(String(32), default="queued")
    error_code: Mapped[str | None] = mapped_column(String(64))
    suggested_field: Mapped[str | None] = mapped_column(Text)
    final_field: Mapped[str | None] = mapped_column(Text)
    verdict: Mapped[str | None] = mapped_column(String(32))  # strong | relevant | needs_attention | not_useful
    verdict_reason: Mapped[str | None] = mapped_column(Text)
    question: Mapped[str | None] = mapped_column(Text)       # one narrow yes/no question when the AI is unsure
    readable: Mapped[bool] = mapped_column(default=True)
    merchant_confirmed: Mapped[bool] = mapped_column(default=False)   # seller vouched for it despite a flag
    bank_flag: Mapped[str | None] = mapped_column(Text)               # reason from payment.dispute.action_required
    processing_notes: Mapped[list] = mapped_column(JsonType, default=list)
    quality_flags: Mapped[list] = mapped_column(JsonType, default=list)
    mismatches: Mapped[list] = mapped_column(JsonType, default=list)
    original_facts: Mapped[dict] = mapped_column(JsonType, default=dict)
    final_facts: Mapped[dict] = mapped_column(JsonType, default=dict)
    razorpay_document_id: Mapped[str | None] = mapped_column(Text)
    correction_count: Mapped[int] = mapped_column(Integer, default=0)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    run: Mapped[DisputeRun] = relationship(back_populates="evidence")


class MerchantEdit(Base):
    __tablename__ = "merchant_edits"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evidence_items.id", ondelete="CASCADE"))
    fact_key: Mapped[str] = mapped_column(Text)
    old_value: Mapped[object | None] = mapped_column(JsonType)
    new_value: Mapped[object | None] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(Text)
    event_key: Mapped[str] = mapped_column(Text, unique=True)
    event_type: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MetricEvent(Base):
    __tablename__ = "metric_events"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    dispute_id: Mapped[str | None] = mapped_column(Text)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    event_name: Mapped[str] = mapped_column(Text)
    metadata_: Mapped[dict] = mapped_column("metadata", JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LinkToken(Base):
    __tablename__ = "link_tokens"
    jti: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    dispute_id: Mapped[str] = mapped_column(Text)
    merchant_id: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exchanged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    key: Mapped[str] = mapped_column(Text, primary_key=True)          # "<scope>:<client key>"
    request_hash: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int] = mapped_column(Integer)
    response: Mapped[dict] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Notification(Base):
    """Every dispute nudge sent to the merchant, one row per (dispute, kind, channel) so nothing is sent twice.
    The alert link is never stored: it carries a single-use token."""
    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("dispute_id", "kind", "channel", name="uq_notification_once"),)
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    dispute_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(32))        # dispute_created | action_required | reminder_24h | reminder_6h | resolved
    channel: Mapped[str] = mapped_column(String(16))     # in_app | email | whatsapp
    status: Mapped[str] = mapped_column(String(16))      # sent | failed | skipped
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    recipient: Mapped[str | None] = mapped_column(Text)  # masked
    detail: Mapped[str | None] = mapped_column(Text)     # provider message id or error
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PushSubscription(Base):
    """A browser/PWA Web Push endpoint for the merchant (one per installed device)."""
    __tablename__ = "push_subscriptions"
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    merchant_id: Mapped[str] = mapped_column(Text)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(Text)
    auth: Mapped[str] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
