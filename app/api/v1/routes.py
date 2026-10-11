"""Proof Bridge API contract (Guide §9). The browser talks only to these routes."""
from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.clients.disputes import MockDisputeProvider, get_dispute_provider
from app.clients.storage import LocalStorage, get_storage
from app.config import get_settings
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import MetricEvent, Notification, PushSubscription
from app.db.session import get_db
from app.errors import AUTH_INVALID, NOT_FOUND, VALIDATION_FAILED, ProofBridgeError
from app.schemas.api import (ActionRequiredIn, CompleteIn, ContestIn, DraftIn, EvidencePatch, ExchangeIn,
                             MetricIn, PushSubscribeIn, PushUnsubscribeIn, ResolveIn, SeedIn, UploadIn)
from app.security import hmac_verify
from app.security.idempotency import run_idempotent
from app.security.tokens import (Principal, dispute_principal, exchange_link_token, issue_link_token, issue_open_session,
                                 merchant_principal,
                                 require_admin)
from app.services import disputes as dsvc
from app.services import evidence as esvc
from app.services import uploads as usvc
from app.services import merchant as msvc
from app.services import notify as nsvc
from app.services import webhooks as wsvc
from app.services.context import load_context, metric

router = APIRouter()


# ---------- sessions ----------
@router.post("/sessions/exchange", tags=["sessions"])
def exchange(body: ExchangeIn, db: Session = Depends(get_db)):
    out = exchange_link_token(db, body.token)
    metric(db, "link_opened", out["dispute_id"])
    return out


@router.post("/sessions/open", tags=["sessions"])
def open_session():
    """Signs the app in as the demo merchant when DEMO_OPEN_ACCESS=true; 403 otherwise."""
    return issue_open_session()


# ---------- disputes ----------
@router.get("/disputes/{dispute_id}", tags=["disputes"])
def get_dispute(dispute_id: str, _: Principal = Depends(dispute_principal), db: Session = Depends(get_db)):
    return dsvc.view(db, dispute_id)


@router.post("/disputes/{dispute_id}/timer/start", tags=["disputes"])
def start_timer(dispute_id: str, _: Principal = Depends(dispute_principal), db: Session = Depends(get_db)):
    return dsvc.start_timer(db, dispute_id)


@router.get("/disputes/{dispute_id}/coverage", tags=["disputes"])
def get_coverage(dispute_id: str, _: Principal = Depends(dispute_principal), db: Session = Depends(get_db)):
    return dsvc.coverage(db, dispute_id)


@router.patch("/disputes/{dispute_id}/draft", tags=["disputes"])
def patch_draft(dispute_id: str, body: DraftIn, _: Principal = Depends(dispute_principal),
                db: Session = Depends(get_db)):
    return dsvc.save_draft(db, dispute_id, body.summary, body.contested_amount)


@router.post("/disputes/{dispute_id}/contest", tags=["disputes"])
def post_contest(dispute_id: str, body: ContestIn, _: Principal = Depends(dispute_principal),
                 idempotency_key: str | None = Header(default=None), db: Session = Depends(get_db)):
    return run_idempotent(db, f"contest:{dispute_id}", idempotency_key, body.model_dump(),
                          lambda: dsvc.contest(db, dispute_id, body.action))


@router.post("/disputes/{dispute_id}/accept", tags=["disputes"])
def post_accept(dispute_id: str, _: Principal = Depends(dispute_principal),
                idempotency_key: str | None = Header(default=None), db: Session = Depends(get_db)):
    return run_idempotent(db, f"accept:{dispute_id}", idempotency_key, {}, lambda: dsvc.accept(db, dispute_id))


@router.post("/disputes/{dispute_id}/metrics", tags=["disputes"], status_code=204)
def post_metric(dispute_id: str, body: MetricIn, _: Principal = Depends(dispute_principal),
                db: Session = Depends(get_db)):
    metric(db, f"ui:{body.event_name}", dispute_id, **{k: v for k, v in list(body.metadata.items())[:20]})
    return Response(status_code=204)


# ---------- uploads + evidence ----------
@router.post("/disputes/{dispute_id}/uploads", tags=["evidence"], status_code=201)
def create_upload(dispute_id: str, body: UploadIn, _: Principal = Depends(dispute_principal),
                  db: Session = Depends(get_db)):
    dispute = get_dispute_provider().fetch(dispute_id)
    return usvc.create_upload(db, dispute, body.filename, body.mime_type, body.size_bytes)


@router.post("/disputes/{dispute_id}/uploads/{upload_id}/complete", tags=["evidence"], status_code=202)
def complete_upload(dispute_id: str, upload_id: str, bg: BackgroundTasks, body: CompleteIn | None = None,
                    _: Principal = Depends(dispute_principal), db: Session = Depends(get_db)):
    item = usvc.complete_upload(db, dispute_id, upload_id)
    if item.status == "queued":
        bg.add_task(esvc.process_evidence, str(item.id))
    return {"evidence_id": str(item.id), "status": item.status}


@router.get("/disputes/{dispute_id}/evidence/{evidence_id}", tags=["evidence"])
def get_evidence(dispute_id: str, evidence_id: str, _: Principal = Depends(dispute_principal),
                 db: Session = Depends(get_db)):
    item = esvc.get_item(db, dispute_id, evidence_id)
    rule = load_context(dispute_id).rule if item.status == "ready" else None
    return esvc.card(item, rule)


@router.post("/disputes/{dispute_id}/evidence/{evidence_id}/process", tags=["evidence"], status_code=202)
def reprocess(dispute_id: str, evidence_id: str, bg: BackgroundTasks, full: bool = False,
              _: Principal = Depends(dispute_principal), db: Session = Depends(get_db)):
    item = esvc.get_item(db, dispute_id, evidence_id)
    if item.status in ("extracting", "analysing", "validating", "uploading"):
        return {"evidence_id": str(item.id), "status": item.status}
    item.status = "queued"
    db.commit()
    bg.add_task(esvc.process_evidence, str(item.id), full)
    return {"evidence_id": str(item.id), "status": "queued"}


@router.patch("/disputes/{dispute_id}/evidence/{evidence_id}", tags=["evidence"])
def patch_evidence(dispute_id: str, evidence_id: str, body: EvidencePatch,
                   _: Principal = Depends(dispute_principal), db: Session = Depends(get_db)):
    item = esvc.get_item(db, dispute_id, evidence_id)
    if item.status != "ready":
        raise ProofBridgeError(VALIDATION_FAILED, "Wait until this file has finished processing.", 409)
    item = esvc.correct(db, item, body.as_body())
    return esvc.card(item, load_context(dispute_id).rule)


@router.delete("/disputes/{dispute_id}/evidence/{evidence_id}", tags=["evidence"], status_code=204)
def delete_evidence(dispute_id: str, evidence_id: str, _: Principal = Depends(dispute_principal),
                    db: Session = Depends(get_db)):
    esvc.remove(db, esvc.get_item(db, dispute_id, evidence_id))
    return Response(status_code=204)


# ---------- webhooks ----------
@router.post("/webhooks/razorpay", tags=["webhooks"])
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    hmac_verify.verify(body, request.headers.get("x-razorpay-signature"), get_settings().razorpay_webhook_secret)
    event = wsvc.parse(body)
    if not wsvc.record_once(db, "razorpay", request.headers.get("x-razorpay-event-id"), body, event.get("event", "")):
        return {"duplicate": True}
    return wsvc.handle_razorpay(db, event)


@router.post("/webhooks/disputes", tags=["webhooks"])
async def disputes_webhook(request: Request, bg: BackgroundTasks, db: Session = Depends(get_db)):
    body = await request.body()
    hmac_verify.verify(body, request.headers.get("x-razorpay-signature"), get_settings().mock_disputes_webhook_secret)
    event = wsvc.parse(body)
    if not wsvc.record_once(db, "disputes", request.headers.get("x-razorpay-event-id"), body, event.get("event", "")):
        return {"duplicate": True}
    out = wsvc.handle_dispute(db, event)
    entity = (((event.get("payload") or {}).get("dispute")) or {}).get("entity") or {}
    if entity.get("id"):
        msvc.clear_cache()
        bg.add_task(nsvc.run_in_background, event.get("event", ""), entity)   # nudge after replying to the webhook
    return out


# ---------- merchant app tabs (any valid app session) ----------
merchant = APIRouter(prefix="/merchant", tags=["merchant"], dependencies=[Depends(merchant_principal)])


@merchant.get("/profile")
def merchant_profile():
    return msvc.profile()


@merchant.get("/overview")
def merchant_overview(db: Session = Depends(get_db)):
    return msvc.overview(db)


@merchant.get("/payments")
def merchant_payments(days: int = 7, count: int = 50, q: str = ""):
    return msvc.payments(max(1, min(days, 90)), max(1, min(count, 100)), q)


@merchant.get("/payments/{payment_id}")
def merchant_payment(payment_id: str):
    return msvc.payment_detail(payment_id)


@merchant.get("/settlements")
def merchant_settlements(days: int = 7):
    return msvc.settlements(max(1, min(days, 90)))


@merchant.get("/products")
def merchant_products():
    return msvc.products()


@merchant.get("/disputes")
def merchant_disputes(db: Session = Depends(get_db)):
    return msvc.disputes_list(db)


@merchant.get("/notifications")
def merchant_notifications(limit: int = 50, db: Session = Depends(get_db)):
    rows = db.scalars(select(Notification).order_by(Notification.created_at.desc()).limit(max(1, min(limit, 200))))
    items = [nsvc.serialize(n) for n in rows]
    return {"items": items, "unread": sum(1 for n in items if n["channel"] == "in_app" and not n["read"])}


@merchant.post("/notifications/read", status_code=204)
def merchant_notifications_read(dispute_id: str | None = None, db: Session = Depends(get_db)):
    """Marks in-app alerts read: all of them, or only one dispute's when the seller opens that dispute."""
    q = select(Notification).where(Notification.channel == "in_app", Notification.read_at.is_(None))
    if dispute_id:
        q = q.where(Notification.dispute_id == dispute_id)
    for n in db.scalars(q):
        n.read_at = datetime.now(timezone.utc)
    db.commit()
    return Response(status_code=204)


@merchant.get("/push/key")
def push_key():
    """The VAPID public key the app needs to subscribe; null when phone notifications aren't set up."""
    return {"public_key": get_settings().vapid_public_key or None}


@merchant.post("/push/subscribe", status_code=201)
def push_subscribe(body: PushSubscribeIn, request: Request, p: Principal = Depends(merchant_principal),
                   db: Session = Depends(get_db)):
    if not body.endpoint.startswith("https://"):
        raise ProofBridgeError(VALIDATION_FAILED, "Push endpoint must be https", 422)
    row = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    if row is None:
        row = PushSubscription(endpoint=body.endpoint)
        db.add(row)
    row.merchant_id, row.p256dh, row.auth = p.merchant_id, body.keys.p256dh, body.keys.auth
    row.user_agent = (request.headers.get("user-agent") or "")[:300]
    db.commit()
    return {"subscribed": True}


@merchant.post("/push/unsubscribe", status_code=204)
def push_unsubscribe(body: PushUnsubscribeIn, db: Session = Depends(get_db)):
    row = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    if row is not None:
        db.delete(row)
        db.commit()
    return Response(status_code=204)


# ---------- reminders (call from a cron every 15–30 min with X-Demo-Secret) ----------
@router.post("/notifications/sweep", tags=["notifications"], dependencies=[Depends(require_admin)])
def notifications_sweep(db: Session = Depends(get_db)):
    return nsvc.sweep(db)


# ---------- demo (admin secret) ----------
demo = APIRouter(prefix="/demo", tags=["demo"], dependencies=[Depends(require_admin)])


def _mock() -> MockDisputeProvider:
    p = get_dispute_provider()
    if not isinstance(p, MockDisputeProvider):
        raise ProofBridgeError(VALIDATION_FAILED, "Demo routes need DISPUTES_PROVIDER=mock", 400)
    return p


def _link(db: Session, dispute_id: str) -> dict:
    token, exp = issue_link_token(db, dispute_id)
    return {"dispute_id": dispute_id, "link": f"{get_settings().frontend_origin.rstrip('/')}/d/{dispute_id}?t={token}",
            "token": token, "expires_at": exp.isoformat()}


@demo.post("/deep-link/{dispute_id}")
def demo_link(dispute_id: str, db: Session = Depends(get_db)):
    get_dispute_provider().fetch(dispute_id)      # 404s cleanly if the dispute doesn't exist
    return _link(db, dispute_id)


@demo.post("/seed")
def demo_seed(body: SeedIn, bg: BackgroundTasks, db: Session = Depends(get_db)):
    """Create a fresh mock dispute on a real captured test payment and return its alert link."""
    pid = body.payment_id
    if not pid:
        last = db.query(MetricEvent).filter(MetricEvent.event_name == "payment_captured") \
            .order_by(MetricEvent.id.desc()).first()
        pid = (last.metadata_ or {}).get("payment_id") if last else None
    if not pid:
        raise ProofBridgeError(VALIDATION_FAILED, "Pass payment_id (a captured rzp test payment).", 422)
    d = _mock().create(pid, body.reason_code, body.respond_in_hours, body.amount)
    dsvc.reset_local(db, d["id"])
    msvc.clear_cache()
    # the alert also goes out without the mock's webhook; the webhook path dedupes against this one
    bg.add_task(nsvc.run_in_background, "seed", d)
    return {"dispute": dsvc.dispute_summary(d), **_link(db, d["id"])}


@demo.post("/disputes/{dispute_id}/nudge")
def demo_nudge(dispute_id: str, kind: str = "dispute_created", db: Session = Depends(get_db)):
    """Send (or resend failed/skipped channels of) one nudge now and return what each channel did."""
    return {"dispute_id": dispute_id, "kind": kind, "results": nsvc.nudge(db, get_dispute_provider().fetch(dispute_id), kind)}


@demo.post("/reset/{dispute_id}")
def demo_reset(dispute_id: str, db: Session = Depends(get_db)):
    """Clear local Proof Bridge state for one dispute (the mock dispute itself is untouched)."""
    dsvc.reset_local(db, dispute_id)
    return {"reset": dispute_id}


@demo.post("/disputes/{dispute_id}/action-required")
def demo_action_required(dispute_id: str, body: ActionRequiredIn):
    return _mock().action_required(dispute_id, body.field, body.reason, body.respond_in_hours)


@demo.post("/disputes/{dispute_id}/resolve")
def demo_resolve(dispute_id: str, body: ResolveIn):
    return _mock().resolve(dispute_id, body.outcome)


@demo.get("/metrics/{dispute_id}")
def demo_metrics(dispute_id: str, db: Session = Depends(get_db)):
    rows = db.query(MetricEvent).filter(MetricEvent.dispute_id == dispute_id).order_by(MetricEvent.id).all()
    return [{"event": r.event_name, "at": r.created_at.isoformat(), **(r.metadata_ or {})} for r in rows]


# ---------- local storage stand-in for R2 (only when STORAGE_PROVIDER=local) ----------
dev_storage = APIRouter(prefix="/dev-storage", tags=["dev"], include_in_schema=False)


def _local() -> LocalStorage:
    st = get_storage()
    if not isinstance(st, LocalStorage):
        raise ProofBridgeError(NOT_FOUND, "Not found", 404)
    return st


@dev_storage.put("/{key:path}")
async def dev_put(key: str, exp: int, sig: str, request: Request):
    st = _local()
    if not st.verify("PUT", key, exp, sig):
        raise ProofBridgeError(AUTH_INVALID, "Signature expired or invalid", 403)
    data = await request.body()
    if len(data) > get_settings().max_upload_bytes:
        raise ProofBridgeError("FILE_TOO_LARGE", "File too large", 413)
    st.put_bytes(key, data, request.headers.get("content-type", "application/octet-stream"))
    return Response(status_code=200, headers={"ETag": f'"{len(data)}"'})


@dev_storage.get("/{key:path}")
def dev_get(key: str, exp: int, sig: str):
    st = _local()
    if not st.verify("GET", key, exp, sig):
        raise ProofBridgeError(AUTH_INVALID, "Signature expired or invalid", 403)
    head = st.head(key)
    if head is None:
        raise ProofBridgeError(NOT_FOUND, "Not found", 404)
    return Response(st.get_bytes(key), media_type=head["content_type"] or "application/octet-stream")


router.include_router(demo)
router.include_router(merchant)
router.include_router(dev_storage)
