"""Dispute provider abstraction. The mock (live on Vercel) and real Razorpay share one contract:
GET /v1/disputes/:id · PATCH /v1/disputes/:id/contest · POST /v1/disputes/:id/accept.
Switching to production = DISPUTES_PROVIDER=razorpay. Mock-only extras are used by demo routes."""
from typing import Protocol

import httpx

from app.config import get_settings
from app.errors import (DISPUTE_ALREADY_SUBMITTED, DISPUTE_DEADLINE_PASSED, DISPUTE_PROVIDER_ERROR,
                        NOT_FOUND, ProofBridgeError)


class DisputeProvider(Protocol):
    def fetch(self, dispute_id: str) -> dict: ...
    def contest(self, dispute_id: str, payload: dict) -> dict: ...
    def accept(self, dispute_id: str) -> dict: ...


def _map_error(res: httpx.Response) -> ProofBridgeError:
    try:
        desc = res.json().get("error", {}).get("description", "") or ""
    except ValueError:
        desc = res.text[:200]
    low = desc.lower()
    if "does not exist" in low:
        return ProofBridgeError(NOT_FOUND, "Dispute not found", 404)
    if "deadline" in low:
        return ProofBridgeError(DISPUTE_DEADLINE_PASSED, desc, 409)
    if "not allowed when dispute is in" in low:
        return ProofBridgeError(DISPUTE_ALREADY_SUBMITTED, desc, 409)
    return ProofBridgeError(DISPUTE_PROVIDER_ERROR, desc or f"Dispute provider returned {res.status_code}",
                            502 if res.status_code >= 500 else 400)


class HttpDisputeProvider:
    """Talks to anything with Razorpay's Disputes API shape (the mock or api.razorpay.com)."""

    def __init__(self, base_url: str, key_id: str, key_secret: str, transport: httpx.BaseTransport | None = None):
        self._c = httpx.Client(base_url=base_url.rstrip("/"), auth=(key_id, key_secret), timeout=20.0,
                               transport=transport)

    def _call(self, method: str, path: str, **kw) -> dict:
        try:
            res = self._c.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise ProofBridgeError(DISPUTE_PROVIDER_ERROR, f"Dispute provider unreachable: {e.__class__.__name__}", 502)
        if res.status_code >= 400:
            raise _map_error(res)
        return res.json()

    def fetch(self, dispute_id: str) -> dict:
        return self._call("GET", f"/v1/disputes/{dispute_id}")

    def contest(self, dispute_id: str, payload: dict) -> dict:
        return self._call("PATCH", f"/v1/disputes/{dispute_id}/contest", json=payload)

    def accept(self, dispute_id: str) -> dict:
        return self._call("POST", f"/v1/disputes/{dispute_id}/accept")

    def list(self, count: int = 50) -> list[dict]:
        return self._call("GET", "/v1/disputes", params={"count": count}).get("items", [])


class MockDisputeProvider(HttpDisputeProvider):
    """Adds the mock-only routes from razorpay-mock-disputes (demo setup and side paths)."""

    def create(self, payment_id: str, reason_code: str = "goods_not_received", respond_in_hours: float = 72,
               amount: int | None = None) -> dict:
        body = {"payment_id": payment_id, "reason_code": reason_code, "respond_in_hours": respond_in_hours}
        if amount:
            body["amount"] = amount
        return self._call("POST", "/v1/disputes", json=body)

    def action_required(self, dispute_id: str, field: str | None, reason: str | None, hours: float = 48) -> dict:
        return self._call("POST", f"/mock/disputes/{dispute_id}/action-required",
                          json={"field": field, "reason": reason, "respond_in_hours": hours})

    def resolve(self, dispute_id: str, outcome: str) -> dict:
        return self._call("POST", f"/mock/disputes/{dispute_id}/resolve", json={"outcome": outcome})


_provider: HttpDisputeProvider | None = None


def get_dispute_provider() -> HttpDisputeProvider:
    global _provider
    if _provider is None:
        s = get_settings()
        if s.disputes_provider == "razorpay":
            # api.razorpay.com/v1 base already includes /v1 in our paths, so strip it
            _provider = HttpDisputeProvider(s.razorpay_base_url.removesuffix("/v1"), s.razorpay_key_id,
                                            s.razorpay_key_secret)
        else:
            _provider = MockDisputeProvider(s.mock_disputes_base_url, s.razorpay_key_id, s.razorpay_key_secret)
    return _provider
