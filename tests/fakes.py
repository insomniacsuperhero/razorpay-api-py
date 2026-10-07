"""In-memory stand-ins for Razorpay test API and the mock disputes service (same request/response shapes)."""
import json
import secrets
import time

import httpx


class FakeRazorpay:
    def __init__(self):
        now = int(time.time()) - 5 * 86400
        self.payments = {"pay_TEST001": {"id": "pay_TEST001", "entity": "payment", "amount": 249900, "currency": "INR",
                                         "status": "captured", "method": "upi", "contact": "+919000000001",
                                         "email": "riya.test@example.com", "order_id": None, "created_at": now,
                                         "notes": {"order_id": "PB-1001"}}}
        self.documents: dict[str, dict] = {}
        self.calls: list[str] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        path, m = req.url.path.removeprefix("/v1"), req.method
        self.calls.append(f"{m} {path}")
        if m == "GET" and path == "/payments":
            return httpx.Response(200, json={"entity": "collection", "items": sorted(self.payments.values(), key=lambda p: -p["created_at"])})
        if m == "GET" and path == "/settlements":
            return httpx.Response(200, json={"entity": "collection", "items": []})
        if m == "GET" and path == "/payment_links":
            return httpx.Response(200, json={"payment_links": [{"id": "plink_1", "amount": 249900, "status": "paid",
                                                                "description": "Kurta", "short_url": "https://rzp.io/i/x"}]})
        if m == "GET" and path == "/invoices":
            return httpx.Response(200, json={"entity": "collection", "items": []})
        if m == "GET" and path.startswith("/payments/"):
            p = self.payments.get(path.split("/")[2])
            return httpx.Response(200, json=p) if p else httpx.Response(400, json={"error": {"description": "The id provided does not exist"}})
        if m == "PATCH" and path.startswith("/payments/"):
            p = self.payments[path.split("/")[2]]
            p["notes"] = json.loads(req.content)["notes"]
            return httpx.Response(200, json=p)
        if m == "POST" and path == "/documents":
            body = req.content
            assert b'name="purpose"' in body and b"dispute_evidence" in body
            mime = "application/pdf" if b"Content-Type: application/pdf" in body else "image/jpeg"
            assert b"%PDF" in body or b"\xff\xd8\xff" in body, "Razorpay only accepts JPG/PNG/PDF"
            doc = {"id": "doc_" + secrets.token_hex(7), "entity": "document", "purpose": "dispute_evidence",
                   "mime_type": mime, "size": len(body), "created_at": int(time.time())}
            self.documents[doc["id"]] = doc
            return httpx.Response(200, json=doc)
        return httpx.Response(404, json={"error": {"description": "unknown route"}})


class FakeMockDisputes:
    """Mirrors razorpay-mock-disputes lib/core.mjs closely enough for contract tests."""
    FIELDS = ["shipping_proof", "billing_proof", "cancellation_proof", "customer_communication", "proof_of_service",
              "explanation_letter", "refund_confirmation", "access_activity_log", "refund_cancellation_policy",
              "term_and_conditions"]

    def __init__(self, rzp: FakeRazorpay):
        self.rzp, self.disputes, self.contest_calls = rzp, {}, []

    def _err(self, msg, code=400):
        return httpx.Response(code, json={"error": {"code": "BAD_REQUEST_ERROR", "description": msg}})

    def handler(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        body = json.loads(req.content or b"{}") if req.content else {}
        if m == "POST" and p == "/v1/disputes":
            pay = self.rzp.payments.get(body["payment_id"])
            d = {"id": "disp_" + secrets.token_hex(7), "entity": "dispute", "payment_id": body["payment_id"],
                 "amount": body.get("amount") or pay["amount"], "currency": "INR", "amount_deducted": 0,
                 "reason_code": body.get("reason_code", "goods_not_received"), "status": "open", "phase": "chargeback",
                 "respond_by": int(time.time() + body.get("respond_in_hours", 72) * 3600), "created_at": int(time.time()),
                 "evidence": {"amount": None, "summary": None, **{f: None for f in self.FIELDS}, "others": None,
                              "submitted_at": None}, "_mock": {}}
            self.disputes[d["id"]] = d
            return httpx.Response(200, json=d)
        if m == "GET" and p == "/v1/disputes":
            items = sorted(self.disputes.values(), key=lambda d: -d["created_at"])
            return httpx.Response(200, json={"entity": "collection", "count": len(items), "items": items})
        parts = p.strip("/").split("/")
        d = self.disputes.get(parts[2]) if len(parts) > 2 else None
        if d is None:
            return self._err("The id provided does not exist")
        if m == "GET" and len(parts) == 3:
            return httpx.Response(200, json=d)
        if m == "PATCH" and parts[3] == "contest":
            self.contest_calls.append(body)
            if d["status"] in ("lost", "won", "closed", "under_review"):
                return self._err(f"Action not allowed when dispute is in {d['status']} status.")
            if time.time() > d["respond_by"]:
                return self._err("Action not allowed as deadline to respond has elapsed.")
            if body.get("amount") and body["amount"] > d["amount"]:
                return self._err("contest amount cannot be greater than dispute amount.")
            if len(body.get("summary") or "") > 1000:
                return self._err("summary may not be greater than 1000 characters.")
            extra = set(body) - {"amount", "summary", "action", "others", *self.FIELDS}
            if extra:
                return self._err(f"{', '.join(extra)} is/are not required and should not be sent.")
            for f in self.FIELDS:
                if f in body:
                    assert all(str(x).startswith("doc_") for x in body[f])
                    d["evidence"][f] = body[f]
            d["evidence"]["summary"], d["evidence"]["amount"] = body.get("summary"), body.get("amount")
            if body.get("action") == "submit":
                d["status"], d["evidence"]["submitted_at"] = "under_review", int(time.time())
            return httpx.Response(200, json=d)
        if m == "POST" and parts[3] == "accept":
            if d["status"] != "open":
                return self._err(f"Action not allowed when dispute is in {d['status']} status.")
            d["status"], d["amount_deducted"] = "lost", d["amount"]
            return httpx.Response(200, json=d)
        if m == "POST" and parts[0] == "mock" and parts[3] == "action-required":
            d["status"], d["_mock"]["action_required"] = "open", {"field": body.get("field"), "reason": body.get("reason")}
            return httpx.Response(200, json=d)
        return self._err("Unknown route", 404)
