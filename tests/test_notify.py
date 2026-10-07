"""Dispute nudges (in-app, email, WhatsApp) and the merchant app endpoints."""
import json
import time

import httpx

from app.clients import messaging
from app.config import get_settings
from tests.conftest import ADMIN, seed_and_login


def _settings(**kw):
    s = get_settings()
    old = {k: getattr(s, k) for k in kw}
    for k, v in kw.items():
        object.__setattr__(s, k, v)
    return old


def test_seed_sends_in_app_and_skips_unconfigured_channels(env):
    client, *_ = env
    did, auth, _ = seed_and_login(client)
    r = client.get("/api/v1/merchant/notifications", headers=auth).json()
    by = {n["channel"]: n for n in r["items"] if n["dispute_id"] == did}
    assert by["in_app"]["status"] == "sent" and by["in_app"]["title"] == "Dispute on ₹2,499"
    assert by["email"]["status"] == "skipped" and "RESEND_API_KEY" in by["email"]["detail"]
    assert by["whatsapp"]["status"] == "skipped"
    assert r["unread"] >= 1
    assert client.post("/api/v1/merchant/notifications/read", headers=auth).status_code == 204
    assert client.get("/api/v1/merchant/notifications", headers=auth).json()["unread"] == 0


def test_email_and_whatsapp_carry_a_working_single_use_link(env):
    client, *_ = env
    sent = []

    def resend(req: httpx.Request):
        sent.append(("email", json.loads(req.content)))
        return httpx.Response(200, json={"id": "email_123"})

    def meta(req: httpx.Request):
        sent.append(("whatsapp", json.loads(req.content)))
        return httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})

    old = _settings(resend_api_key="re_test", merchant_email="seller@example.com", whatsapp_token="t",
                    whatsapp_phone_number_id="123", merchant_whatsapp="+91 98123 45678")
    messaging.email_sender, messaging.whatsapp_sender = (messaging.EmailSender(httpx.MockTransport(resend)),
                                                          messaging.WhatsAppSender(httpx.MockTransport(meta)))
    try:
        did, auth, _ = seed_and_login(client)
        kinds = [k for k, _ in sent]
        assert kinds.count("email") == 1 and kinds.count("whatsapp") == 1
        email = next(b for k, b in sent if k == "email")
        assert email["to"] == ["seller@example.com"] and "Dispute on ₹2,499" in email["subject"]
        link = email["text"].split("Add proof: ")[1].strip()
        token = link.split("?t=")[1]
        assert f"/d/{did}?t=" in link
        assert client.post("/api/v1/sessions/exchange", json={"token": token}).status_code == 200
        assert client.post("/api/v1/sessions/exchange", json={"token": token}).status_code == 401   # single use
        wa = next(b for k, b in sent if k == "whatsapp")
        assert wa["to"] == "919812345678" and wa["type"] == "text" and "/d/" in wa["text"]["body"]
        # the same nudge is never sent twice
        r = client.post(f"/api/v1/demo/disputes/{did}/nudge", headers=ADMIN).json()
        assert {x["status"] for x in r["results"] if x["channel"] in ("in_app", "email", "whatsapp")} == {"already_sent"}
        notes = client.get("/api/v1/merchant/notifications", headers=auth).json()["items"]
        assert all("?t=" not in (n["body"] + (n["detail"] or "")) for n in notes)   # tokens never stored
    finally:
        _settings(**old)
        messaging.email_sender, messaging.whatsapp_sender = messaging.EmailSender(), messaging.WhatsAppSender()


def test_failed_channel_is_recorded_and_retried(env):
    client, *_ = env
    old = _settings(resend_api_key="re_test", merchant_email="seller@example.com")
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        return httpx.Response(500, json={"message": "down"}) if calls["n"] == 1 else httpx.Response(200, json={"id": "e2"})
    messaging.email_sender = messaging.EmailSender(httpx.MockTransport(flaky))
    try:
        did, auth, _ = seed_and_login(client)
        r = client.get("/api/v1/merchant/notifications", headers=auth).json()["items"]
        assert next(n for n in r if n["channel"] == "email")["status"] == "failed"
        r = client.post(f"/api/v1/demo/disputes/{did}/nudge", headers=ADMIN).json()
        assert next(x for x in r["results"] if x["channel"] == "email")["status"] == "sent"
    finally:
        _settings(**old)
        messaging.email_sender = messaging.EmailSender()


def test_sweep_sends_the_tightest_reminder_once(env):
    client, _, mock = env
    did, auth, _ = seed_and_login(client, hours=5)
    r = client.post("/api/v1/notifications/sweep", headers=ADMIN).json()
    kinds = [s["kind"] for s in r["sent"] if s["dispute"] == did]
    assert kinds == ["reminder_6h"]
    r = client.post("/api/v1/notifications/sweep", headers=ADMIN).json()
    assert not [s for s in r["sent"] if s["dispute"] == did]


def test_merchant_tabs_read_razorpay(env):
    client, rzp, _ = env
    did, auth, _ = seed_and_login(client)
    rzp.payments["pay_TEST001"]["created_at"] = int(time.time()) - 60
    from app.services import merchant
    merchant.clear_cache()
    o = client.get("/api/v1/merchant/overview", headers=auth).json()
    assert o["collected_today"]["amount"] == 249900 and len(o["week"]["days"]) == 7
    assert o["recent"][0]["dispute"]["id"] == did and o["disputes_open"] == 1
    p = client.get("/api/v1/merchant/payments?q=pay_TEST001", headers=auth).json()["items"][0]
    assert p["contact_masked"] == "•••••0001" and "contact" not in p
    assert client.get("/api/v1/merchant/payments/pay_NOPE", headers=auth).status_code == 404
    d = client.get("/api/v1/merchant/disputes", headers=auth).json()["items"]
    assert d[0]["id"] == did and d[0]["reason_label"]
    pr = client.get("/api/v1/merchant/products", headers=auth).json()
    assert pr["payment_links"]["count"] == 1
    assert client.get("/api/v1/merchant/overview").status_code == 401


def test_web_push_reaches_subscribed_devices_and_drops_dead_ones(env):
    client, *_ = env
    sent = []

    class FakePush(messaging.PushSender):
        def send_one(self, sub, payload):
            sent.append((sub["endpoint"], payload))
            return 410 if "dead" in sub["endpoint"] else 201
    old = _settings(vapid_public_key="pub", vapid_private_key="priv")
    messaging.push_sender = FakePush()
    try:
        did, auth, _ = seed_and_login(client)          # no devices yet: push is skipped
        assert client.get("/api/v1/merchant/push/key", headers=auth).json()["public_key"] == "pub"
        for ep in ("https://fcm.googleapis.com/fcm/send/live", "https://web.push.apple.com/dead"):
            r = client.post("/api/v1/merchant/push/subscribe", headers=auth, json={"endpoint": ep, "keys": {"p256dh": "k", "auth": "a"}})
            assert r.status_code == 201
        assert client.post("/api/v1/merchant/push/subscribe", headers=auth,
                           json={"endpoint": "http://evil", "keys": {"p256dh": "k", "auth": "a"}}).status_code == 422
        r = client.post(f"/api/v1/demo/disputes/{did}/nudge", headers=ADMIN).json()
        push = next(x for x in r["results"] if x["channel"] == "push")
        assert push["status"] == "sent"
        payload = sent[-1][1]
        assert payload["title"] == "Dispute on ₹2,499" and payload["url"].startswith(f"/d/{did}?t=")
        token = payload["url"].split("?t=")[1]
        assert client.post("/api/v1/sessions/exchange", json={"token": token}).status_code == 200
        prof = client.get("/api/v1/merchant/profile", headers=auth).json()
        assert prof["channels"]["push"]["devices"] == 1          # the dead endpoint was removed
    finally:
        _settings(**old)
        messaging.push_sender = messaging.PushSender()


def test_open_session_only_when_enabled(env):
    client, *_ = env
    assert client.post("/api/v1/sessions/open").status_code == 403
    old = _settings(demo_open_access=True)
    try:
        r = client.post("/api/v1/sessions/open")
        assert r.status_code == 200 and r.json()["dispute_id"] is None
        auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
        assert client.get("/api/v1/merchant/disputes", headers=auth).status_code == 200
        did, _, _ = seed_and_login(client)
        assert client.get(f"/api/v1/disputes/{did}", headers=auth).status_code == 200
    finally:
        _settings(**old)
