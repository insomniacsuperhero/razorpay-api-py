"""End-to-end flow following Guide §14.2 test order, with Razorpay and the mock replaced by in-memory fakes."""
import json
import uuid
from datetime import date, timedelta

from app.security.hmac_verify import sign
from tests import samples
from tests.conftest import ADMIN, seed_and_login, upload


def test_health_has_no_secrets(env):
    client, *_ = env
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "secret" not in r.text.lower().replace("razorpay_configured", "")


def test_link_token_is_single_use_and_scoped(env):
    client, *_ = env
    did, auth, seeded = seed_and_login(client)
    r = client.post("/api/v1/sessions/exchange", json={"token": seeded["token"]})
    assert r.status_code == 401 and r.json()["error"]["code"] == "AUTH_EXPIRED"
    other, _, _ = seed_and_login(client)
    r = client.get(f"/api/v1/disputes/{other}", headers=auth)      # the app session is the merchant's
    assert r.status_code == 200
    from jose import jwt
    foreign = jwt.encode({"typ": "access", "sub": "someone_else", "dispute_id": did, "exp": 9999999999},
                         "dev-only-change-me-dev-only-change-me", algorithm="HS256")
    r = client.get(f"/api/v1/disputes/{did}", headers={"Authorization": f"Bearer {foreign}"})
    assert r.status_code in (401, 403)
    r = client.get(f"/api/v1/disputes/{did}")
    assert r.status_code == 401
    assert seeded["link"].startswith("http://localhost:5173/d/")


def test_dispute_view_uses_real_payment_context(env):
    client, rzp, _ = env
    did, auth, _ = seed_and_login(client)
    v = client.get(f"/api/v1/disputes/{did}", headers=auth).json()
    assert v["payment_verified"] is True
    assert v["payment"]["contact_masked"] == "•••••0001"
    assert v["payment"]["email_masked"] == "r•••@example.com"
    assert v["dispute"]["reason_label"] == "Customer says the order never arrived"
    assert v["requirements"]["required"][0]["field"] == "shipping_proof"
    assert "GET /payments/pay_TEST001" in rzp.calls


def test_critical_path_courier_screenshot_to_real_doc_id(env):
    """Guide's critical-path milestone: screenshot -> facts -> matched -> Strong shipping_proof -> doc_ id."""
    client, rzp, _ = env
    did, auth, _ = seed_and_login(client)
    client.post(f"/api/v1/disputes/{did}/timer/start", headers=auth)
    card = upload(client, did, auth, "delhivery_tracking.jpg", samples.jpeg(), "image/jpeg")
    assert card["status"] == "ready", card
    assert card["field"] == "shipping_proof" and card["verdict"] == "strong"
    assert card["razorpay_document_id"].startswith("doc_")
    assert card["razorpay_document_id"] in rzp.documents
    assert {f["key"] for f in card["facts"]} >= {"tracking_id", "courier"}
    assert card["preview_url"]


def test_irrelevant_photo_is_not_useful_and_not_uploaded(env):
    client, rzp, _ = env
    did, auth, _ = seed_and_login(client)
    card = upload(client, did, auth, "product_photo.png", samples.png(), "image/png")
    assert card["verdict"] == "not_useful" and card["razorpay_document_id"] is None
    assert not rzp.documents


def test_blurry_image_needs_attention(env):
    client, *_ = env
    did, auth, _ = seed_and_login(client)
    card = upload(client, did, auth, "courier_blurry.jpg", samples.jpeg(), "image/jpeg")
    assert card["verdict"] == "needs_attention" and card["razorpay_document_id"] is None
    assert "blurry" in card["reason"].lower()


def test_amount_mismatch_then_merchant_confirms(env):
    client, rzp, _ = env
    did, auth, _ = seed_and_login(client)
    data = samples.pdf(["Invoice No: INV-2041", "Order: PB-1001", "Total: Rs. 1,999"])
    card = upload(client, did, auth, "invoice.pdf", data, "application/pdf")
    assert card["field"] == "billing_proof" and card["verdict"] == "needs_attention"
    assert card["mismatches"][0]["fact"] == "amount_paise"
    assert card["razorpay_document_id"] is None
    r = client.patch(f"/api/v1/disputes/{did}/evidence/{card['id']}", headers=auth, json={"confirm": True})
    c2 = r.json()
    assert c2["verdict"] == "relevant" and c2["razorpay_document_id"].startswith("doc_")
    assert rzp.documents[c2["razorpay_document_id"]]["mime_type"] == "application/pdf"


def test_docx_is_converted_to_pdf_for_razorpay(env):
    client, rzp, _ = env
    did, auth, _ = seed_and_login(client)
    data = samples.docx(["WhatsApp chat export", "Customer: \"Received, thank you! Loved the kurta\""])
    card = upload(client, did, auth, "chat_export.docx", data,
                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert card["detected_mime_type"].endswith("wordprocessingml.document")
    assert card["field"] == "customer_communication" and card["verdict"] == "relevant"
    assert rzp.documents[card["razorpay_document_id"]]["mime_type"] == "application/pdf"
    assert "Word file converted to PDF for Razorpay" in card["notes"]


def test_disguised_file_is_rejected_by_magic_bytes(env):
    client, *_ = env
    did, auth, _ = seed_and_login(client)
    card = upload(client, did, auth, "invoice.pdf", b"MZ\x90\x00 this is an exe" * 10, "application/pdf")
    assert card["status"] == "failed" and card["error"]["code"] == "UNSUPPORTED_FILE_TYPE"


def test_upload_type_and_size_checks(env):
    client, *_ = env
    did, auth, _ = seed_and_login(client)
    r = client.post(f"/api/v1/disputes/{did}/uploads", headers=auth,
                    json={"filename": "x.zip", "mime_type": "application/zip", "size_bytes": 10})
    assert r.status_code == 415
    r = client.post(f"/api/v1/disputes/{did}/uploads", headers=auth,
                    json={"filename": "x.pdf", "mime_type": "application/pdf", "size_bytes": 10**10})
    assert r.status_code == 413
    r = client.post(f"/api/v1/disputes/{did}/uploads/{uuid.uuid4()}/complete", headers=auth, json={})
    assert r.status_code == 404


def test_complete_without_upload_is_r2_missing(env):
    client, *_ = env
    did, auth, _ = seed_and_login(client)
    up = client.post(f"/api/v1/disputes/{did}/uploads", headers=auth,
                     json={"filename": "a.jpg", "mime_type": "image/jpeg", "size_bytes": 100}).json()
    r = client.post(f"/api/v1/disputes/{did}/uploads/{up['upload_id']}/complete", headers=auth, json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "R2_UPLOAD_MISSING"


def test_merchant_correction_keeps_original_and_logs(env):
    client, *_ = env
    did, auth, _ = seed_and_login(client)
    card = upload(client, did, auth, "courier_tracking.jpg", samples.jpeg(), "image/jpeg")
    new_date = (date.today() - timedelta(days=2)).isoformat()
    r = client.patch(f"/api/v1/disputes/{did}/evidence/{card['id']}", headers=auth,
                     json={"fact_key": "delivery_date", "value": new_date})
    c2 = r.json()
    assert c2["correction_count"] == 1
    fact = next(f for f in c2["facts"] if f["key"] == "delivery_date")
    assert fact["value"] == new_date and fact["edited"] is True and fact["matches_payment"] is True
    # a date before the payment contradicts it -> Needs attention
    r = client.patch(f"/api/v1/disputes/{did}/evidence/{card['id']}", headers=auth,
                     json={"fact_key": "delivery_date", "value": "2020-01-01"})
    assert r.json()["verdict"] == "needs_attention"
    r = client.patch(f"/api/v1/disputes/{did}/evidence/{card['id']}", headers=auth,
                     json={"fact_key": "not_a_fact", "value": "x"})
    assert r.status_code == 422


def test_coverage_draft_submit_idempotent(env):
    client, rzp, mock = env
    did, auth, _ = seed_and_login(client)
    client.post(f"/api/v1/disputes/{did}/timer/start", headers=auth)
    cov = client.get(f"/api/v1/disputes/{did}/coverage", headers=auth).json()
    assert cov["complete"] is False and cov["label"] == "Missing required proof"
    r = client.post(f"/api/v1/disputes/{did}/contest", headers={**auth, "Idempotency-Key": "k0"},
                    json={"action": "submit"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "NO_EVIDENCE_TO_SUBMIT"

    courier = upload(client, did, auth, "delhivery_tracking.jpg", samples.jpeg(), "image/jpeg")
    upload(client, did, auth, "whatsapp_chat.jpg", samples.jpeg(), "image/jpeg")
    junk = upload(client, did, auth, "product_photo.jpg", samples.jpeg(), "image/jpeg")
    cov = client.get(f"/api/v1/disputes/{did}/coverage", headers=auth).json()
    assert cov["complete"] is True and cov["label"] == "Good coverage"
    assert "tracking ID DL78421135" in cov["summary"]["draft"]
    assert "Received, thank you!" in cov["summary"]["draft"]
    assert len(cov["summary"]["draft"]) <= 1000

    r = client.patch(f"/api/v1/disputes/{did}/draft", headers=auth, json={"contested_amount": 999999999})
    assert r.status_code == 422
    edited = cov["summary"]["draft"] + " Seller adds: customer collected in person."
    r = client.patch(f"/api/v1/disputes/{did}/draft", headers=auth, json={"summary": edited})
    assert r.json()["summary"]["edited"] is True

    r = client.post(f"/api/v1/disputes/{did}/contest", headers=auth, json={"action": "submit"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    r = client.post(f"/api/v1/disputes/{did}/contest", headers={**auth, "Idempotency-Key": "k1"},
                    json={"action": "draft"})
    assert r.status_code == 200 and r.json()["dispute"]["status"] == "open"

    r = client.post(f"/api/v1/disputes/{did}/contest", headers={**auth, "Idempotency-Key": "k2"},
                    json={"action": "submit"})
    body = r.json()
    assert r.status_code == 200 and body["dispute"]["status"] == "under_review"
    assert body["sent"]["shipping_proof"] == [courier["razorpay_document_id"]]
    assert "customer_communication" in body["sent"] and junk["id"] not in json.dumps(body["sent"])
    assert body["sent"]["summary"] == edited and body["sent"]["amount"] == 249900
    assert body["seconds_to_submit"] is not None
    calls = len(mock.contest_calls)
    r2 = client.post(f"/api/v1/disputes/{did}/contest", headers={**auth, "Idempotency-Key": "k2"},
                     json={"action": "submit"})
    assert r2.status_code == 200 and r2.headers.get("Idempotent-Replay") == "true"
    assert len(mock.contest_calls) == calls, "retry must not resubmit"
    r3 = client.post(f"/api/v1/disputes/{did}/contest", headers={**auth, "Idempotency-Key": "k3"},
                     json={"action": "submit"})
    assert r3.status_code == 409 and r3.json()["error"]["code"] == "DISPUTE_ALREADY_SUBMITTED"


def test_remove_evidence_excludes_it(env):
    client, *_ = env
    did, auth, _ = seed_and_login(client)
    c = upload(client, did, auth, "courier_tracking.jpg", samples.jpeg(), "image/jpeg")
    assert client.delete(f"/api/v1/disputes/{did}/evidence/{c['id']}", headers=auth).status_code == 204
    cov = client.get(f"/api/v1/disputes/{did}/coverage", headers=auth).json()
    assert cov["complete"] is False
    assert client.get(f"/api/v1/disputes/{did}", headers=auth).json()["evidence"] == []


def test_accept_side_path(env):
    client, *_ = env
    did, auth, _ = seed_and_login(client)
    r = client.post(f"/api/v1/disputes/{did}/accept", headers={**auth, "Idempotency-Key": "a1"})
    assert r.status_code == 200 and r.json()["dispute"]["status"] == "lost"
    r = client.post(f"/api/v1/disputes/{did}/accept", headers={**auth, "Idempotency-Key": "a2"})
    assert r.status_code == 409


def test_deadline_passed_blocks_contest(env):
    client, *_ = env
    did, auth, _ = seed_and_login(client, hours=-1)
    v = client.get(f"/api/v1/disputes/{did}", headers=auth).json()
    assert v["dispute"]["deadline_passed"] is True and v["dispute"]["can_contest"] is False
    r = client.post(f"/api/v1/disputes/{did}/contest", headers={**auth, "Idempotency-Key": "d1"},
                    json={"action": "draft"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "DISPUTE_DEADLINE_PASSED"


def _signed(client, path, event: dict, secret: str, event_id: str | None = None, bad=False):
    body = json.dumps(event).encode()
    headers = {"Content-Type": "application/json", "X-Razorpay-Signature": "bad" if bad else sign(body, secret)}
    if event_id:
        headers["X-Razorpay-Event-Id"] = event_id
    return client.post(path, content=body, headers=headers)


def test_dispute_webhook_action_required_flags_card_and_dedupes(env):
    client, _, mock = env
    did, auth, _ = seed_and_login(client)
    c = upload(client, did, auth, "courier_tracking.jpg", samples.jpeg(), "image/jpeg")
    mock.disputes[did]["status"] = "under_review"
    d = client.post(f"/api/v1/demo/disputes/{did}/action-required", headers=ADMIN, json={}).json()
    event = {"entity": "event", "event": "payment.dispute.action_required", "payload": {"dispute": {"entity": d}}}
    assert _signed(client, "/api/v1/webhooks/disputes", event, "mock_whsec", bad=True).status_code == 401
    r = _signed(client, "/api/v1/webhooks/disputes", event, "mock_whsec", "evt_1")
    assert r.status_code == 200 and r.json()["handled"] == "payment.dispute.action_required"
    r = _signed(client, "/api/v1/webhooks/disputes", event, "mock_whsec", "evt_1")
    assert r.json() == {"duplicate": True}
    card = client.get(f"/api/v1/disputes/{did}/evidence/{c['id']}", headers=auth).json()
    assert card["verdict"] == "needs_attention" and card["bank_flag"]
    v = client.get(f"/api/v1/disputes/{did}", headers=auth).json()
    assert v["dispute"]["bank_request"]["field"] == "shipping_proof"


def test_razorpay_webhook_feeds_demo_seed(env):
    client, *_ = env
    event = {"entity": "event", "event": "payment_link.paid",
             "payload": {"payment": {"entity": {"id": "pay_TEST001", "amount": 249900}},
                         "payment_link": {"entity": {"id": "plink_1"}}}}
    r = _signed(client, "/api/v1/webhooks/razorpay", event, "rzp_whsec", "evt_pay_1")
    assert r.json()["payment_id"] == "pay_TEST001"
    r = client.post("/api/v1/demo/seed", headers=ADMIN, json={})     # no payment_id: uses the webhook's
    assert r.status_code == 200 and r.json()["dispute"]["payment_id"] == "pay_TEST001"
    assert client.post("/api/v1/demo/seed", json={}).status_code == 401


def test_metrics_recorded(env):
    client, *_ = env
    did, auth, _ = seed_and_login(client)
    client.post(f"/api/v1/disputes/{did}/timer/start", headers=auth)
    assert client.post(f"/api/v1/disputes/{did}/metrics", headers=auth,
                       json={"event_name": "screen_2_enter"}).status_code == 204
    names = [m["event"] for m in client.get(f"/api/v1/demo/metrics/{did}", headers=ADMIN).json()]
    assert {"link_opened", "timer_started", "ui:screen_2_enter"} <= set(names)


def test_empty_admin_secret_never_opens_demo_routes(env, monkeypatch):
    client, *_ = env
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "demo_admin_secret", "")
    assert client.post("/api/v1/demo/seed", json={}).status_code == 401
    assert client.post("/api/v1/demo/seed", headers={"X-Demo-Secret": ""}, json={}).status_code == 401


def test_exchange_tolerates_whitespace(env):
    client, *_ = env
    r = client.post("/api/v1/demo/seed", headers=ADMIN, json={"payment_id": "pay_TEST001"})
    tok = r.json()["token"]
    r = client.post("/api/v1/sessions/exchange", json={"token": f"  {tok}\n"})
    assert r.status_code == 200 and r.json()["access_token"]


def test_stuck_card_becomes_retryable(env):
    from datetime import datetime, timedelta, timezone
    from app.db.models import EvidenceItem
    from app.db.session import SessionLocal
    client, *_ = env
    did, auth, _ = seed_and_login(client)
    card = upload(client, did, auth, "courier_tracking.jpg", samples.jpeg(), "image/jpeg")
    db = SessionLocal()
    it = db.get(EvidenceItem, uuid.UUID(card["id"]))
    it.status, it.updated_at = "analysing", datetime.now(timezone.utc) - timedelta(minutes=10)
    db.commit(); db.close()
    cov = client.get(f"/api/v1/disputes/{did}/coverage", headers=auth).json()
    assert card["id"] not in cov["processing"]          # coverage marks the stuck card too
    c = client.get(f"/api/v1/disputes/{did}/evidence/{card['id']}", headers=auth).json()
    assert c["status"] == "failed" and c["error"]["code"] == "PROCESSING_INTERRUPTED"
    assert client.post(f"/api/v1/disputes/{did}/evidence/{card['id']}/process", headers=auth).status_code == 202
    c = client.get(f"/api/v1/disputes/{did}/evidence/{card['id']}", headers=auth).json()
    assert c["status"] == "ready"
