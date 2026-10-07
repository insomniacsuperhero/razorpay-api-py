"""Dispute nudges to the merchant: in-app, email and WhatsApp, from one place.

Triggers
  payment.dispute.created          -> "dispute_created"  (also sent by /demo/seed, so it works without the webhook)
  payment.dispute.action_required  -> "action_required"
  payment.dispute.won / lost       -> "resolved"
  /notifications/sweep (cron)      -> "reminder_24h" / "reminder_6h" while no response has been submitted,
                                      plus any "dispute_created" a missed webhook never sent

Each (dispute, kind, channel) is sent at most once. A channel that isn't configured is recorded as
"skipped" and retried on the next trigger, so adding a key later starts delivery without duplicates.
Email and WhatsApp carry a single-use link (valid 24 h) that opens the dispute screen directly.
"""
import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients import messaging
from app.clients.disputes import get_dispute_provider
from app.config import get_settings
from app.db.models import DisputeRun, Notification, PushSubscription
from app.domain.evidence_rules import rule_for
from app.domain.matcher import rupees
from app.security.tokens import issue_link_token
from app.services.context import metric

log = logging.getLogger("proofbridge.notify")
IST = ZoneInfo("Asia/Kolkata")
CHANNELS = ("in_app", "push", "email", "whatsapp")


def _when(ts: int | None) -> str:
    if not ts:
        return "soon"
    return datetime.fromtimestamp(ts, tz=IST).strftime("%a %-d %b, %-I:%M %p")


def compose(kind: str, d: dict) -> dict:
    """Plain words, same facts on every channel. Returns title, text, cta and the 4 template params."""
    amount, reason, deadline = rupees(int(d.get("amount") or 0)), rule_for(d.get("reason_code", "")).reason_label, _when(d.get("respond_by"))
    bank = ((d.get("_mock") or {}).get("action_required") or {}).get("reason")
    if kind == "dispute_created":
        title = f"Dispute on {amount}"
        text = f"{reason}. Respond by {deadline}.\nAdd proof from your phone; it takes about 2 minutes."
    elif kind == "action_required":
        title = f"The bank needs more proof for the {amount} dispute"
        text = f"{bank or 'The evidence was unclear.'} Respond by {deadline}."
        reason = bank or reason
    elif kind.startswith("reminder_"):
        hours = kind.removeprefix("reminder_").removesuffix("h")
        title = f"{hours} hours left to answer the {amount} dispute"
        text = f"{reason}. Respond by {deadline}, or the amount may be deducted."
    else:
        won = d.get("status") == "won"
        title = f"Dispute {'won' if won else 'closed'}: {amount}"
        text = ("The bank decided in your favour. Nothing was deducted." if won
                else f"The dispute is {d.get('status')}. {amount} was deducted." if d.get("status") == "lost"
                else f"The dispute is {d.get('status')}.")
    cta = "View dispute" if kind == "resolved" else "Add proof"
    return {"title": title, "text": text, "cta": cta, "params": [amount, reason, deadline]}


def _enabled() -> list[str]:
    return [c.strip() for c in get_settings().notify_channels.split(",") if c.strip() in CHANNELS]


def _link(db: Session, dispute_id: str) -> str:
    s = get_settings()
    token, _ = issue_link_token(db, dispute_id, minutes=s.alert_link_minutes)
    return f"{s.frontend_origin.rstrip('/')}/d/{dispute_id}?t={token}"


def nudge(db: Session, dispute: dict, kind: str) -> list[dict]:
    """Send one kind of nudge on every enabled channel. Safe to call repeatedly."""
    s = get_settings()
    msg = compose(kind, dispute)
    out = []
    for channel in _enabled():
        row = db.scalar(select(Notification).where(Notification.dispute_id == dispute["id"],
                                                   Notification.kind == kind, Notification.channel == channel))
        if row is not None and row.status == "sent":
            out.append({"channel": channel, "status": "already_sent"})
            continue
        status, detail, recipient = "sent", None, None
        try:
            if channel == "in_app":
                recipient = "Razorpay app"
            elif channel == "push":
                status, detail, recipient = _push(db, dispute, kind, msg)
            elif channel == "email":
                recipient = messaging.mask_email(s.merchant_email)
                if not messaging.EmailSender.configured():
                    status, detail = "skipped", "Set RESEND_API_KEY and MERCHANT_EMAIL to send email."
                else:
                    link = None if kind == "resolved" else _link(db, dispute["id"])
                    detail = messaging.email_sender.send(msg["title"], msg["text"], link, msg["cta"])
            elif channel == "whatsapp":
                recipient = messaging.mask_phone(s.merchant_whatsapp)
                if not messaging.WhatsAppSender.configured():
                    status, detail = "skipped", "Set WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID and MERCHANT_WHATSAPP."
                else:
                    link = None if kind == "resolved" else _link(db, dispute["id"])
                    text = f"*{msg['title']}*\n{msg['text']}"
                    detail = messaging.whatsapp_sender.send(text, msg["params"] + [link or ""], link)
        except messaging.MessageError as e:
            status, detail = "failed", str(e)[:500]
            log.warning("nudge %s %s via %s failed: %s", kind, dispute["id"], channel, e)
        if row is None:
            row = Notification(dispute_id=dispute["id"], kind=kind, channel=channel)
            db.add(row)
        row.status, row.title, row.body, row.recipient, row.detail = status, msg["title"], msg["text"], recipient, detail
        row.created_at = datetime.now(timezone.utc)
        db.commit()
        out.append({"channel": channel, "status": status, "detail": detail})
    metric(db, "nudge", dispute["id"], kind=kind, results={o["channel"]: o["status"] for o in out})
    return out


def _push(db: Session, dispute: dict, kind: str, msg: dict) -> tuple[str, str | None, str | None]:
    """Web Push to every subscribed device. The notification opens the dispute through a fresh single-use link,
    so it works even when the app was closed and its session expired."""
    if not messaging.PushSender.configured():
        return "skipped", "Set VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY to send phone notifications.", None
    subs = db.scalars(select(PushSubscription).where(PushSubscription.merchant_id == get_settings().merchant_id)).all()
    if not subs:
        return "skipped", "No phone has turned on notifications yet (Account > Notification).", "0 devices"
    url = f"/d/{dispute['id']}" if kind == "resolved" else _link(db, dispute["id"]).split(get_settings().frontend_origin.rstrip('/'), 1)[-1]
    payload = {"title": msg["title"], "body": msg["text"].replace("\n", " "), "url": url, "tag": f"dispute-{dispute['id']}"}
    ok, errors = 0, []
    for sub in subs:
        try:
            code = messaging.push_sender.send_one({"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}, payload)
            if code in (404, 410):
                db.delete(sub)              # the browser unsubscribed or the app was removed
            else:
                ok += 1
        except messaging.MessageError as e:
            errors.append(str(e))
    db.commit()
    if ok:
        return "sent", None, f"{ok} device{'s' if ok != 1 else ''}"
    return ("failed", "; ".join(errors)[:500], f"{len(subs)} device(s)") if errors else \
        ("skipped", "Every saved device had unsubscribed.", "0 devices")


def on_dispute_event(db: Session, event: str, dispute: dict) -> list[dict]:
    kind = {"payment.dispute.created": "dispute_created", "payment.dispute.action_required": "action_required",
            "payment.dispute.won": "resolved", "payment.dispute.lost": "resolved"}.get(event)
    return nudge(db, dispute, kind) if kind else []


def sweep(db: Session) -> dict:
    """Run every 15–30 min from a cron (e.g. cron-job.org). Sends reminders and catches missed alerts."""
    hours = sorted({int(h) for h in get_settings().reminder_hours.split(",") if h.strip().isdigit()})
    now, sent = time.time(), []
    for d in get_dispute_provider().list(100):
        if d.get("status") == "open":
            if db.scalar(select(Notification.id).where(Notification.dispute_id == d["id"],
                                                       Notification.kind.in_(["dispute_created", "action_required"]))) is None:
                sent.append({"dispute": d["id"], "kind": "dispute_created", "results": nudge(db, d, "dispute_created")})
            left_h = ((d.get("respond_by") or 0) - now) / 3600
            run = db.get(DisputeRun, d["id"])
            due = [h for h in hours if 0 < left_h <= h]
            if due and not (run and run.submitted_at):
                kind = f"reminder_{due[0]}h"        # the tightest window only, never two at once
                done = db.scalar(select(Notification.id).where(Notification.dispute_id == d["id"],
                                                               Notification.kind == kind, Notification.status == "sent"))
                if done is None:
                    sent.append({"dispute": d["id"], "kind": kind, "results": nudge(db, d, kind)})
    return {"checked_at": int(now), "sent": sent}


def run_in_background(event: str, dispute: dict) -> None:
    """For BackgroundTasks: own DB session, never raises."""
    from app.db.session import SessionLocal
    db = SessionLocal()
    try:
        if event == "seed":
            nudge(db, dispute, "dispute_created")
        else:
            on_dispute_event(db, event, dispute)
    except Exception:   # a failed nudge must never break the webhook or the demo
        log.exception("nudge for %s failed", dispute.get("id"))
    finally:
        db.close()


def serialize(n: Notification) -> dict:
    return {"id": n.id, "dispute_id": n.dispute_id, "kind": n.kind, "channel": n.channel, "status": n.status,
            "title": n.title, "body": n.body, "recipient": n.recipient,
            "detail": n.detail if n.status != "sent" else None,
            "read": n.read_at is not None, "created_at": n.created_at.isoformat() if n.created_at else None}
