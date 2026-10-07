"""Dispute view, coverage, draft, contest and accept. The contest payload is rebuilt from the
database and the trusted dispute — never from document IDs, fields or amounts sent by the browser."""
import re
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.disputes import get_dispute_provider
from app.db.models import DisputeRun, EvidenceItem, MetricEvent
from app.domain.coverage import compute_coverage
from app.domain.evidence_rules import FIELD_LABELS
from app.domain.matcher import rupees
from app.domain.summary import MAX as SUMMARY_MAX, build_summary
from app.errors import (DISPUTE_ALREADY_SUBMITTED, DISPUTE_DEADLINE_PASSED, NO_EVIDENCE_TO_SUBMIT,
                        VALIDATION_FAILED, ProofBridgeError)
from app.services.context import DisputeContext, ensure_run, load_context, metric
from app.services.evidence import card

FINAL_STATES = {"won", "lost", "closed", "under_review"}


def _mask_phone(p: str | None) -> str | None:
    d = re.sub(r"\D", "", p or "")
    return f"•••••{d[-4:]}" if len(d) >= 4 else None


def _mask_email(e: str | None) -> str | None:
    if not e or "@" not in e:
        return None
    name, dom = e.split("@", 1)
    return f"{name[:1]}•••@{dom}"


def active_items(db: Session, dispute_id: str) -> list[EvidenceItem]:
    return list(db.scalars(select(EvidenceItem).where(EvidenceItem.dispute_id == dispute_id,
                                                      EvidenceItem.removed_at.is_(None))
                           .order_by(EvidenceItem.created_at)))


def dispute_summary(d: dict) -> dict:
    now = int(time.time())
    bank = (d.get("_mock") or {}).get("action_required")
    return {
        "id": d["id"], "payment_id": d["payment_id"], "status": d["status"], "phase": d.get("phase"),
        "amount": d["amount"], "amount_display": rupees(int(d["amount"])), "currency": d.get("currency", "INR"),
        "reason_code": d.get("reason_code"), "respond_by": d.get("respond_by"),
        "respond_by_iso": datetime.fromtimestamp(d["respond_by"], tz=timezone.utc).isoformat() if d.get("respond_by") else None,
        "seconds_left": max(0, d["respond_by"] - now) if d.get("respond_by") else None,
        "deadline_passed": bool(d.get("respond_by") and now > d["respond_by"]),
        "can_contest": d["status"] == "open" and not (d.get("respond_by") and now > d["respond_by"]),
        "bank_request": bank,
        "evidence_on_record": {k: v for k, v in (d.get("evidence") or {}).items() if v},
    }


def view(db: Session, dispute_id: str) -> dict:
    c = load_context(dispute_id)
    run = ensure_run(db, c.dispute)
    p = c.payment or {}
    return {
        "dispute": {**dispute_summary(c.dispute), "reason_label": c.rule.reason_label},
        "payment": None if not c.payment else {
            "id": p["id"], "amount": p.get("amount"), "amount_display": rupees(int(p.get("amount") or 0)),
            "status": p.get("status"), "method": p.get("method"),
            "created_at": datetime.fromtimestamp(int(p.get("created_at", 0)), tz=timezone.utc).isoformat(),
            "contact_masked": _mask_phone(p.get("contact")), "email_masked": _mask_email(p.get("email")),
            "order_id": p.get("order_id"),
            "order_ref": (p.get("notes") or {}).get("order_id") if isinstance(p.get("notes"), dict) else None,
        },
        "payment_verified": c.payment is not None, "payment_error": c.payment_error,
        "requirements": {
            "required": [{"field": f, "label": FIELD_LABELS[f]} for f in c.rule.required],
            "helpful": [{"field": f, "label": FIELD_LABELS[f]} for f in c.rule.helpful],
            "hint": c.rule.hint,
        },
        "evidence": [card(i, c.rule) for i in active_items(db, dispute_id)],
        "run": {"timer_started_at": run.timer_started_at.isoformat() if run.timer_started_at else None,
                "submitted_at": run.submitted_at.isoformat() if run.submitted_at else None,
                "draft_summary": run.draft_summary, "contested_amount": run.contested_amount},
    }


def coverage(db: Session, dispute_id: str, c: DisputeContext | None = None) -> dict:
    c = c or load_context(dispute_id)
    run = ensure_run(db, c.dispute)
    items = [i for i in active_items(db, dispute_id) if i.status == "ready"]
    cov = compute_coverage(c.rule, items)
    generated = build_summary(c.ctx, c.rule.reason_label, items)
    if cov["complete"] and not db.scalar(select(MetricEvent.id).where(
            MetricEvent.dispute_id == dispute_id, MetricEvent.event_name == "evidence_complete").limit(1)):
        metric(db, "evidence_complete", dispute_id, label=cov["label"])
    pending = [str(i.id) for i in active_items(db, dispute_id) if i.status not in ("ready", "failed")]
    return {**cov, "processing": pending,
            "summary": {"draft": run.draft_summary if run.summary_edited and run.draft_summary else generated,
                        "generated": generated, "edited": run.summary_edited, "max_length": SUMMARY_MAX},
            "contested_amount": run.contested_amount or int(c.dispute["amount"]),
            "disputed_amount": int(c.dispute["amount"])}


def save_draft(db: Session, dispute_id: str, summary: str | None, contested_amount: int | None) -> dict:
    c = load_context(dispute_id)
    run = ensure_run(db, c.dispute)
    if summary is not None:
        if len(summary) > SUMMARY_MAX:
            raise ProofBridgeError(VALIDATION_FAILED, f"Summary must be {SUMMARY_MAX} characters or fewer.", 422)
        run.draft_summary, run.summary_edited = summary, True
    if contested_amount is not None:
        if contested_amount <= 0 or contested_amount > int(c.dispute["amount"]):
            raise ProofBridgeError(VALIDATION_FAILED, "Contested amount must be between 1 paise and the disputed amount.", 422)
        run.contested_amount = contested_amount
    db.commit()
    return coverage(db, dispute_id, c)


def build_contest_payload(db: Session, c: DisputeContext, action: str) -> dict:
    run = ensure_run(db, c.dispute)
    items = [i for i in active_items(db, c.dispute["id"]) if i.status == "ready"]
    cov = compute_coverage(c.rule, items)
    payload: dict = {}
    for fld, doc_ids in cov["fields_with_documents"].items():
        if fld == "others":
            payload["others"] = [{"type": "supporting_evidence", "document_ids": doc_ids}]
        else:
            payload[fld] = doc_ids
    payload["summary"] = (run.draft_summary if run.summary_edited and run.draft_summary
                          else build_summary(c.ctx, c.rule.reason_label, items))
    payload["amount"] = run.contested_amount or int(c.dispute["amount"])
    payload["action"] = action
    return payload


def contest(db: Session, dispute_id: str, action: str) -> dict:
    if action not in ("draft", "submit"):
        raise ProofBridgeError(VALIDATION_FAILED, "action must be draft or submit", 422)
    c = load_context(dispute_id)
    s = dispute_summary(c.dispute)
    if c.dispute["status"] in FINAL_STATES:
        raise ProofBridgeError(DISPUTE_ALREADY_SUBMITTED, f"This dispute is already {c.dispute['status']}.", 409)
    if s["deadline_passed"]:
        raise ProofBridgeError(DISPUTE_DEADLINE_PASSED, "The deadline to respond has passed.", 409)
    payload = build_contest_payload(db, c, action)
    n_docs = sum(len(v) for k, v in payload.items() if isinstance(v, list) and k != "others") + \
        sum(len(o["document_ids"]) for o in payload.get("others", []))
    if action == "submit" and n_docs == 0:
        raise ProofBridgeError(NO_EVIDENCE_TO_SUBMIT, "Add at least one useful piece of proof before submitting.", 422)
    result = get_dispute_provider().contest(dispute_id, payload)
    run = ensure_run(db, c.dispute)
    out = {"dispute": dispute_summary(result), "sent": {k: v for k, v in payload.items()}, "documents": n_docs}
    if action == "submit":
        run.submitted_at = datetime.now(timezone.utc)
        db.commit()
        secs = None
        if run.timer_started_at:
            started = run.timer_started_at if run.timer_started_at.tzinfo else run.timer_started_at.replace(tzinfo=timezone.utc)
            secs = round((run.submitted_at - started).total_seconds(), 1)
        metric(db, "submitted", dispute_id, seconds_to_submit=secs, documents=n_docs)
        out["seconds_to_submit"] = secs
    else:
        metric(db, "draft_saved", dispute_id, documents=n_docs)
    return out


def accept(db: Session, dispute_id: str) -> dict:
    c = load_context(dispute_id)
    if c.dispute["status"] != "open":
        raise ProofBridgeError(DISPUTE_ALREADY_SUBMITTED, f"This dispute is already {c.dispute['status']}.", 409)
    result = get_dispute_provider().accept(dispute_id)
    metric(db, "accepted", dispute_id, amount=int(c.dispute["amount"]))
    return {"dispute": dispute_summary(result)}


def start_timer(db: Session, dispute_id: str) -> dict:
    c = load_context(dispute_id)
    run = ensure_run(db, c.dispute)
    if run.timer_started_at is None or run.submitted_at is not None:   # restart after a bank request
        run.timer_started_at, run.submitted_at = datetime.now(timezone.utc), None
        db.commit()
        metric(db, "timer_started", dispute_id)
    return {"timer_started_at": run.timer_started_at.isoformat()}


def reset_local(db: Session, dispute_id: str) -> None:
    from app.db.models import MerchantEdit, UploadSession
    ids = [i for (i,) in db.query(EvidenceItem.id).filter(EvidenceItem.dispute_id == dispute_id)]
    if ids:   # explicit order keeps Postgres foreign keys happy
        db.query(MerchantEdit).filter(MerchantEdit.evidence_id.in_(ids)).delete(synchronize_session=False)
    db.query(EvidenceItem).filter(EvidenceItem.dispute_id == dispute_id).delete(synchronize_session=False)
    db.query(UploadSession).filter(UploadSession.dispute_id == dispute_id).delete(synchronize_session=False)
    db.query(DisputeRun).filter(DisputeRun.dispute_id == dispute_id).delete(synchronize_session=False)
    db.query(MetricEvent).filter(MetricEvent.dispute_id == dispute_id).delete(synchronize_session=False)
    db.commit()
    db.expire_all()
