"""Webhook signature: X-Razorpay-Signature = hex HMAC-SHA256(raw body, secret). Verified before any parsing."""
import hashlib
import hmac

from app.errors import INVALID_SIGNATURE, ProofBridgeError


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(body: bytes, signature: str | None, secret: str) -> None:
    if not secret:
        raise ProofBridgeError(INVALID_SIGNATURE, "Webhook secret not configured on the server.", 503)
    if not signature or not hmac.compare_digest(sign(body, secret), signature):
        raise ProofBridgeError(INVALID_SIGNATURE, "Invalid webhook signature.", 401)
