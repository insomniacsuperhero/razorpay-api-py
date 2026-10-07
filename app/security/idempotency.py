"""Idempotency-Key handling (Guide §13.2): same key + same request -> the stored response, no re-run.
Protects submit/accept/processing from weak-network retries."""
import hashlib
import json
from collections.abc import Callable

from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import IdempotencyKey
from app.errors import IDEMPOTENCY_KEY_REQUIRED, IDEMPOTENCY_KEY_REUSED, ProofBridgeError


def run_idempotent(db: Session, scope: str, key: str | None, request_body: dict,
                   fn: Callable[[], dict], status: int = 200) -> JSONResponse:
    if not key or len(key) > 200:
        raise ProofBridgeError(IDEMPOTENCY_KEY_REQUIRED, "Send an Idempotency-Key header (a fresh UUID per action).", 400)
    full = f"{scope}:{key}"
    req_hash = hashlib.sha256(json.dumps(request_body, sort_keys=True, default=str).encode()).hexdigest()
    prior = db.get(IdempotencyKey, full)
    if prior:
        if prior.request_hash != req_hash:
            raise ProofBridgeError(IDEMPOTENCY_KEY_REUSED, "This Idempotency-Key was used for a different request.", 422)
        return JSONResponse(prior.response, status_code=prior.status_code, headers={"Idempotent-Replay": "true"})
    result = fn()   # errors propagate and are NOT stored, so the client can retry with the same key
    db.add(IdempotencyKey(key=full, request_hash=req_hash, status_code=status, response=result))
    try:
        db.commit()
    except IntegrityError:          # a concurrent duplicate won the race; return its result
        db.rollback()
        prior = db.get(IdempotencyKey, full)
        return JSONResponse(prior.response, status_code=prior.status_code, headers={"Idempotent-Replay": "true"})
    return JSONResponse(result, status_code=status)
