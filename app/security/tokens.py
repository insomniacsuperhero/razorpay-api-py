"""Deep-link tokens (single use, short-lived, bound to one dispute) exchanged for an app access token.
Guide §13.1: no long-lived credential stays in the URL."""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header
from jose import ExpiredSignatureError, JWTError, jwt
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import LinkToken
from app.db.session import get_db
from app.errors import AUTH_EXPIRED, AUTH_INVALID, ProofBridgeError

ALG = "HS256"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def issue_link_token(db: Session, dispute_id: str, merchant_id: str | None = None,
                     minutes: int | None = None) -> tuple[str, datetime]:
    s = get_settings()
    merchant_id = merchant_id or s.merchant_id
    jti, exp = uuid.uuid4(), _now() + timedelta(minutes=minutes or s.link_token_minutes)
    db.add(LinkToken(jti=jti, dispute_id=dispute_id, merchant_id=merchant_id, expires_at=exp))
    db.commit()
    token = jwt.encode({"typ": "link", "sub": merchant_id, "dispute_id": dispute_id, "jti": str(jti),
                        "exp": int(exp.timestamp())}, s.jwt_signing_secret, algorithm=ALG)
    return token, exp


def _decode(token: str, typ: str) -> dict:
    try:
        claims = jwt.decode(token, get_settings().jwt_signing_secret, algorithms=[ALG])
    except ExpiredSignatureError:
        raise ProofBridgeError(AUTH_EXPIRED, "This link has expired. Open the latest alert again.", 401)
    except JWTError:
        raise ProofBridgeError(AUTH_INVALID, "Invalid or tampered token.", 401)
    if claims.get("typ") != typ:
        raise ProofBridgeError(AUTH_INVALID, "Wrong token type.", 401)
    return claims


def exchange_link_token(db: Session, token: str) -> dict:
    c = _decode(token.strip().strip('"'), "link")   # tolerate copy-paste whitespace/quotes
    row = db.get(LinkToken, uuid.UUID(c["jti"]))
    if row is None or row.dispute_id != c["dispute_id"]:
        raise ProofBridgeError(AUTH_INVALID, "Unknown link.", 401)
    if row.exchanged_at is not None:
        raise ProofBridgeError(AUTH_EXPIRED, "This link was already used. Open the latest alert again.", 401)
    row.exchanged_at = _now()
    db.commit()
    s = get_settings()
    exp = _now() + timedelta(minutes=s.access_token_minutes)
    access = jwt.encode({"typ": "access", "sub": c["sub"], "dispute_id": c["dispute_id"],
                         "exp": int(exp.timestamp())}, s.jwt_signing_secret, algorithm=ALG)
    return {"access_token": access, "token_type": "bearer", "expires_at": exp.isoformat(),
            "dispute_id": c["dispute_id"]}


def issue_open_session() -> dict:
    """Merchant session without an alert link (DEMO_OPEN_ACCESS=true): the app opens straight to Home."""
    s = get_settings()
    if not s.demo_open_access:
        raise ProofBridgeError(AUTH_INVALID, "Open the secure link in your dispute alert to sign in.", 403)
    exp = _now() + timedelta(minutes=s.access_token_minutes)
    access = jwt.encode({"typ": "access", "sub": s.merchant_id, "dispute_id": "", "exp": int(exp.timestamp())},
                        s.jwt_signing_secret, algorithm=ALG)
    return {"access_token": access, "token_type": "bearer", "expires_at": exp.isoformat(), "dispute_id": None}


class Principal:
    def __init__(self, merchant_id: str, dispute_id: str):
        self.merchant_id, self.dispute_id = merchant_id, dispute_id

    def require(self, dispute_id: str) -> None:
        """The session is the merchant's app session: it opens any dispute on this merchant's account.
        The prototype runs one Razorpay test account, so every dispute belongs to settings.merchant_id."""
        if self.merchant_id != get_settings().merchant_id:
            raise ProofBridgeError(AUTH_INVALID, "This session is for a different merchant.", 403)


def current_principal(authorization: str = Header(default="")) -> Principal:
    if not authorization.lower().startswith("bearer "):
        raise ProofBridgeError(AUTH_INVALID, "Missing bearer token.", 401)
    c = _decode(authorization.split(" ", 1)[1].strip(), "access")
    return Principal(c["sub"], c["dispute_id"])


def dispute_principal(dispute_id: str, p: Principal = Depends(current_principal)) -> Principal:
    p.require(dispute_id)
    return p


def merchant_principal(p: Principal = Depends(current_principal)) -> Principal:
    if p.merchant_id != get_settings().merchant_id:
        raise ProofBridgeError(AUTH_INVALID, "This session is for a different merchant.", 403)
    return p


def require_admin(x_demo_secret: str = Header(default="")) -> None:
    import hmac
    expected = get_settings().demo_admin_secret
    if not expected or not x_demo_secret or not hmac.compare_digest(x_demo_secret.encode(), expected.encode()):
        raise ProofBridgeError(AUTH_INVALID, "Admin secret required (X-Demo-Secret).", 401)


__all__ = ["issue_link_token", "exchange_link_token", "current_principal", "dispute_principal",
           "issue_open_session", "merchant_principal", "require_admin", "Principal", "get_db"]
