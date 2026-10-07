"""Webhooks: HMAC verified before parsing, deduplicated by event id (or body hash), then handled."""
import hashlib
import json

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import DisputeRun, EvidenceItem, WebhookEvent
from app.services.context import metric


def record_once(db: Session, source: str, event_id: str | None, body: bytes, event_type: str) -> bool:
    """True if new; False if this event was already processed."""
    digest = hashlib.sha256(body).hexdigest()
    db.add(WebhookEvent(source=source, event_key=f"{source}:{event_id or digest}", event_type=event_type,
                        payload_hash=digest))
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def handle_razorpay(db: Session, event: dict) -> dict:
    etype = event.get("event", "")
    payload = event.get("payload") or {}
    if etype in ("payment_link.paid", "payment.captured", "order.paid"):
        pay = ((payload.get("payment") or {}).get("entity")) or {}
        link = ((payload.get("payment_link") or {}).get("entity")) or {}
        metric(db, "payment_captured", None, payment_id=pay.get("id"), amount=pay.get("amount"),
               payment_link_id=link.get("id"), notes=pay.get("notes") or {})
        return {"handled": etype, "payment_id": pay.get("id")}
    return {"ignored": etype}


def handle_dispute(db: Session, event: dict) -> dict:
    etype = event.get("event", "")
    d = (((event.get("payload") or {}).get("dispute")) or {}).get("entity") or {}
    if not d.get("id"):
        return {"ignored": etype}
    run = db.get(DisputeRun, d["id"])
    if run is None:
        run = DisputeRun(dispute_id=d["id"], payment_id=d.get("payment_id", ""))
        db.add(run)
        db.commit()
    if etype == "payment.dispute.created":
        metric(db, "alert_created", d["id"], reason=d.get("reason_code"), respond_by=d.get("respond_by"))
    elif etype == "payment.dispute.action_required":
        req = (d.get("_mock") or {}).get("action_required") or {}
        fld, reason = req.get("field"), req.get("reason") or "Please upload clearer proof."
        q = db.query(EvidenceItem).filter(EvidenceItem.dispute_id == d["id"], EvidenceItem.removed_at.is_(None))
        flagged = 0
        for it in q:
            if fld is None or it.final_field == fld:
                it.bank_flag, it.verdict = reason, "needs_attention"
                it.verdict_reason = f"The bank asked for clearer proof: {reason}"
                flagged += 1
        run.submitted_at = None          # back to the seller; the timer restarts on the next "Add proof"
        db.commit()
        metric(db, "bank_requested_more", d["id"], field=fld, flagged=flagged)
    else:
        metric(db, etype.replace("payment.dispute.", "dispute_"), d["id"], status=d.get("status"))
    return {"handled": etype, "dispute_id": d["id"]}


def parse(body: bytes) -> dict:
    return json.loads(body.decode() or "{}")
