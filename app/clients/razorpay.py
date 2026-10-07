"""Real Razorpay Test Mode API (server-side Basic auth). Refuses anything but rzp_test_ keys."""
import httpx

from app.config import get_settings
from app.errors import RAZORPAY_DOCUMENT_UPLOAD_FAILED, RAZORPAY_UNAVAILABLE, ProofBridgeError


class RazorpayClient:
    def __init__(self, key_id: str, key_secret: str, base_url: str, transport: httpx.BaseTransport | None = None):
        if key_id and not key_id.startswith("rzp_test_"):
            raise RuntimeError("Proof Bridge prototype only runs with Razorpay test keys (rzp_test_...)")
        self._client = httpx.Client(base_url=base_url, auth=(key_id, key_secret), timeout=30.0, transport=transport)
        self.configured = bool(key_id and key_secret)

    def _request(self, method: str, path: str, error_code: str = RAZORPAY_UNAVAILABLE, **kw) -> dict:
        if not self.configured:
            raise ProofBridgeError(RAZORPAY_UNAVAILABLE, "Razorpay test keys are not configured", 503)
        try:
            res = self._client.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise ProofBridgeError(error_code, f"Razorpay unreachable: {e.__class__.__name__}", 502)
        if res.status_code >= 400:
            desc = (res.json().get("error", {}) or {}).get("description") if res.headers.get(
                "content-type", "").startswith("application/json") else res.text[:200]
            raise ProofBridgeError(error_code, f"Razorpay {res.status_code}: {desc}", 502 if res.status_code >= 500 else 400)
        return res.json()

    # --- reads used for trusted payment context ---
    def fetch_payment(self, payment_id: str) -> dict:
        return self._request("GET", f"/payments/{payment_id}")

    def fetch_order(self, order_id: str) -> dict:
        return self._request("GET", f"/orders/{order_id}")

    # --- demo setup ---
    def create_payment_link(self, amount_paise: int, description: str, notes: dict | None = None) -> dict:
        return self._request("POST", "/payment_links", json={
            "amount": amount_paise, "currency": "INR", "description": description,
            "notify": {"sms": False, "email": False}, "notes": notes or {}})

    def update_payment_notes(self, payment_id: str, notes: dict) -> dict:
        return self._request("PATCH", f"/payments/{payment_id}", json={"notes": notes})

    # --- dispute evidence documents (JPG/JPEG/PNG/PDF only) ---
    def upload_document(self, filename: str, data: bytes, mime: str) -> dict:
        return self._request("POST", "/documents", error_code=RAZORPAY_DOCUMENT_UPLOAD_FAILED,
                             data={"purpose": "dispute_evidence"}, files={"file": (filename, data, mime)})

    def fetch_document(self, document_id: str) -> dict:
        return self._request("GET", f"/documents/{document_id}")


_client: RazorpayClient | None = None


def get_razorpay() -> RazorpayClient:
    global _client
    if _client is None:
        s = get_settings()
        _client = RazorpayClient(s.razorpay_key_id, s.razorpay_key_secret, s.razorpay_base_url)
    return _client
