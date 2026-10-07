"""Trusted context for one dispute: the dispute (provider) + the real Razorpay payment (+ order)."""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.clients.disputes import get_dispute_provider
from app.clients.razorpay import get_razorpay
from app.db.models import DisputeRun, MetricEvent
from app.domain.evidence_rules import Rule, rule_for
from app.domain.matcher import PaymentContext
from app.errors import ProofBridgeError

log = logging.getLogger("proofbridge")


@dataclass
class DisputeContext:
    dispute: dict
    payment: dict | None
    payment_error: str | None
    rule: Rule
    ctx: PaymentContext


def load_context(dispute_id: str) -> DisputeContext:
    dispute = get_dispute_provider().fetch(dispute_id)
    payment, order, perr = None, None, None
    try:
        payment = get_razorpay().fetch_payment(dispute["payment_id"])
        if payment.get("order_id"):
            try:
                order = get_razorpay().fetch_order(payment["order_id"])
            except ProofBridgeError:
                order = None
    except ProofBridgeError as e:
        perr = e.message
        log.warning("payment fetch failed for %s: %s", dispute.get("payment_id"), e.message)
    if payment:
        ctx = PaymentContext.from_razorpay(payment, dispute, order)
    else:   # degrade gracefully: match against the dispute's own amount/date, flagged as unverified
        ctx = PaymentContext(payment_id=dispute["payment_id"], amount=int(dispute["amount"]),
                             disputed_amount=int(dispute["amount"]),
                             created_at=datetime.fromtimestamp(int(dispute.get("created_at", 0)), tz=timezone.utc))
    return DisputeContext(dispute, payment, perr, rule_for(dispute.get("reason_code", "")), ctx)


def ensure_run(db: Session, dispute: dict) -> DisputeRun:
    run = db.get(DisputeRun, dispute["id"])
    if run is None:
        run = DisputeRun(dispute_id=dispute["id"], payment_id=dispute["payment_id"])
        db.add(run)
        db.commit()
    return run


def metric(db: Session, name: str, dispute_id: str | None = None, evidence_id=None, **meta) -> None:
    db.add(MetricEvent(dispute_id=dispute_id, evidence_id=evidence_id, event_name=name, metadata_=meta))
    db.commit()
