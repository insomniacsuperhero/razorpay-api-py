"""Read-only data for the merchant app's tabs, straight from the Razorpay test API (and the dispute
provider for Disputes). Contacts and emails are masked before they leave the server."""
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clients.disputes import get_dispute_provider
from app.clients.razorpay import get_razorpay
from app.config import get_settings
from app.db.models import EvidenceItem, Notification
from app.domain.evidence_rules import rule_for
from app.domain.matcher import rupees
from app.errors import NOT_FOUND, ProofBridgeError
from app.services.disputes import _mask_email, _mask_phone, dispute_summary

IST = ZoneInfo("Asia/Kolkata")
_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def clear_cache() -> None:
    _cache.clear()


def _day_start(days_ago: int = 0) -> datetime:
    now = datetime.now(IST)
    return (now - timedelta(days=days_ago)).replace(hour=0, minute=0, second=0, microsecond=0)


def _disputes() -> list[dict]:
    return _cached("disputes", 10, lambda: get_dispute_provider().list(100))


def _disputes_by_payment() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for d in sorted(_disputes(), key=lambda d: d.get("created_at", 0)):
        out[d["payment_id"]] = d          # latest dispute per payment wins
    return out


def payment_view(p: dict, disputes: dict[str, dict] | None = None) -> dict:
    notes = p.get("notes") if isinstance(p.get("notes"), dict) else {}
    d = (disputes or {}).get(p["id"])
    return {
        "id": p["id"], "amount": p.get("amount", 0), "amount_display": rupees(int(p.get("amount") or 0)),
        "currency": p.get("currency", "INR"), "status": p.get("status"), "method": p.get("method"),
        "created_at": datetime.fromtimestamp(int(p.get("created_at", 0)), tz=IST).isoformat(),
        "contact_masked": _mask_phone(p.get("contact")), "email_masked": _mask_email(p.get("email")),
        "description": p.get("description"), "order_id": p.get("order_id"), "order_ref": notes.get("order_id"),
        "fee": p.get("fee"), "tax": p.get("tax"), "refund_status": p.get("refund_status"),
        "amount_refunded": p.get("amount_refunded", 0), "error_description": p.get("error_description"),
        "dispute": {"id": d["id"], "status": d["status"], "reason_label": rule_for(d.get("reason_code", "")).reason_label}
        if d else None,
    }


def payments(days: int = 7, count: int = 50, q: str = "") -> dict:
    q = q.strip()
    disputes = _disputes_by_payment()
    if q.startswith("pay_"):
        try:
            return {"items": [payment_view(get_razorpay().fetch_payment(q), disputes)], "days": days}
        except ProofBridgeError:
            return {"items": [], "days": days}
    frm, to = int(_day_start(days - 1).timestamp()), int(time.time())
    items = _cached(f"payments:{days}:{count}", 15, lambda: get_razorpay().list_payments(frm, to, min(count, 100)))
    views = [payment_view(p, disputes) for p in items]
    if q:
        ql = q.lower()
        views = [v for v in views if ql in " ".join(str(v.get(k) or "") for k in
                                                    ("id", "contact_masked", "email_masked", "description", "order_ref")).lower()]
    return {"items": views, "days": days}


def payment_detail(payment_id: str) -> dict:
    try:
        p = get_razorpay().fetch_payment(payment_id)
    except ProofBridgeError as e:
        raise ProofBridgeError(NOT_FOUND, "Payment not found", 404) from e
    return payment_view(p, _disputes_by_payment())


def overview(db: Session) -> dict:
    frm, to = int(_day_start(6).timestamp()), int(time.time())
    items = _cached("overview:payments", 15, lambda: get_razorpay().list_payments(frm, to, 100))
    captured = [p for p in items if p.get("status") in ("captured", "refunded")]
    today0 = _day_start(0).timestamp()
    days = []
    for i in range(6, -1, -1):
        start, end = _day_start(i).timestamp(), _day_start(i - 1).timestamp() if i else time.time() + 1
        ps = [p for p in captured if start <= p["created_at"] < end]
        days.append({"date": _day_start(i).date().isoformat(), "amount": sum(p["amount"] for p in ps), "count": len(ps)})
    today = [p for p in captured if p["created_at"] >= today0]
    by_method: dict[str, int] = {}
    for p in today:
        by_method[p.get("method") or "other"] = by_method.get(p.get("method") or "other", 0) + p["amount"]
    try:
        stl = _cached("overview:settlements", 60, lambda: get_razorpay().list_settlements(int(_day_start(30).timestamp()), to, 10))
    except ProofBridgeError:
        stl = []
    # Balance the merchant can expect to be settled: captured in the last 30 days, less fees and tax,
    # less what has already been settled (test mode never settles, so this is the unsettled total).
    try:
        month = _cached("overview:payments30", 30, lambda: get_razorpay().list_payments(int(_day_start(29).timestamp()), to, 100))
    except ProofBridgeError:
        month = items
    balance = sum(p["amount"] - (p.get("fee") or 0) for p in month if p.get("status") == "captured") \
        - sum(s_.get("amount", 0) for s_ in stl if s_.get("status") == "processed")
    disputes = _disputes_by_payment()
    open_disputes = [d for d in _disputes() if d.get("status") == "open"]
    return {
        "collected_today": {"amount": sum(p["amount"] for p in today), "count": len(today),
                            "by_method": [{"method": k, "amount": v} for k, v in sorted(by_method.items(), key=lambda x: -x[1])]},
        "week": {"amount": sum(d["amount"] for d in days), "count": sum(d["count"] for d in days), "days": days},
        "recent": [payment_view(p, disputes) for p in items[:5]],
        "last_settlement": settlement_view(stl[0]) if stl else None,
        "balance": max(0, balance),
        "disputes_open": len(open_disputes),
        "unread_notifications": db.scalar(select(func.count(Notification.id)).where(
            Notification.channel == "in_app", Notification.read_at.is_(None))) or 0,
    }


def settlement_view(s: dict) -> dict:
    return {"id": s["id"], "amount": s.get("amount", 0), "amount_display": rupees(int(s.get("amount") or 0)),
            "status": s.get("status"), "utr": s.get("utr"), "fees": s.get("fees"), "tax": s.get("tax"),
            "created_at": datetime.fromtimestamp(int(s.get("created_at", 0)), tz=IST).isoformat()}


def settlements(days: int = 7) -> dict:
    frm, to = int(_day_start(days - 1).timestamp()), int(time.time())
    items = _cached(f"settlements:{days}", 30, lambda: get_razorpay().list_settlements(frm, to, 50))
    return {"items": [settlement_view(s) for s in items], "days": days}


def products() -> dict:
    def safe(fn):
        try:
            return fn()
        except ProofBridgeError:
            return None
    links = safe(lambda: _cached("links", 30, get_razorpay().list_payment_links))
    invoices = safe(lambda: _cached("invoices", 30, lambda: get_razorpay().list_invoices(20)))
    return {
        "payment_links": None if links is None else {
            "count": len(links), "paid": sum(1 for l in links if l.get("status") == "paid"),
            "recent": [{"id": l["id"], "amount_display": rupees(int(l.get("amount") or 0)), "status": l.get("status"),
                        "description": l.get("description"), "short_url": l.get("short_url")} for l in links[:3]]},
        "invoices": None if invoices is None else {
            "count": len(invoices), "paid": sum(1 for i in invoices if i.get("status") == "paid")},
    }


def disputes_list(db: Session) -> dict:
    counts = dict(db.execute(select(EvidenceItem.dispute_id, func.count(EvidenceItem.id))
                             .where(EvidenceItem.removed_at.is_(None)).group_by(EvidenceItem.dispute_id)).all())
    items = []
    for d in sorted(_disputes(), key=lambda d: -d.get("created_at", 0)):
        items.append({**dispute_summary(d), "reason_label": rule_for(d.get("reason_code", "")).reason_label,
                      "evidence_files": counts.get(d["id"], 0),
                      "created_at": datetime.fromtimestamp(int(d.get("created_at", 0)), tz=IST).isoformat()})
    return {"items": items}


def profile() -> dict:
    s = get_settings()
    from app.db.models import PushSubscription
    from app.db.session import SessionLocal
    with SessionLocal() as db:
        devices = db.scalar(select(func.count(PushSubscription.id)).where(PushSubscription.merchant_id == s.merchant_id)) or 0
    return {"name": s.merchant_name, "merchant_id": s.merchant_id, "mode": "test",
            "key_id_hint": (s.razorpay_key_id[:9] + "•••" + s.razorpay_key_id[-4:]) if s.razorpay_key_id else None,
            "channels": {"in_app": True,
                         "push": {"on": "push" in s.notify_channels, "configured": bool(s.vapid_public_key and s.vapid_private_key),
                                  "devices": devices},
                         "email": {"on": "email" in s.notify_channels, "to": _mask_email(s.merchant_email) if s.merchant_email else None,
                                   "configured": bool(s.resend_api_key and s.merchant_email)},
                         "whatsapp": {"on": "whatsapp" in s.notify_channels, "to": _mask_phone(s.merchant_whatsapp) if s.merchant_whatsapp else None,
                                      "configured": bool(s.whatsapp_token and s.whatsapp_phone_number_id and s.merchant_whatsapp)}}}
