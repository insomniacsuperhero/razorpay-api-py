"""Failure contract from the guide (section 17.2). Every error body is {"error": {"code", "message", ...}}."""
from fastapi import Request
from fastapi.responses import JSONResponse


class ProofBridgeError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra


# Codes the frontend can switch on
AUTH_EXPIRED = "AUTH_EXPIRED"
AUTH_INVALID = "AUTH_INVALID"
NOT_FOUND = "NOT_FOUND"
UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
FILE_PROCESSING_FAILED = "FILE_PROCESSING_FAILED"
PROCESSING_INTERRUPTED = "PROCESSING_INTERRUPTED"
EVIDENCE_UNREADABLE = "EVIDENCE_UNREADABLE"
EVIDENCE_PAYMENT_MISMATCH = "EVIDENCE_PAYMENT_MISMATCH"
AI_ANALYSIS_FAILED = "AI_ANALYSIS_FAILED"
R2_UPLOAD_MISSING = "R2_UPLOAD_MISSING"
RAZORPAY_DOCUMENT_UPLOAD_FAILED = "RAZORPAY_DOCUMENT_UPLOAD_FAILED"
RAZORPAY_UNAVAILABLE = "RAZORPAY_UNAVAILABLE"
DISPUTE_PROVIDER_ERROR = "DISPUTE_PROVIDER_ERROR"
DISPUTE_DEADLINE_PASSED = "DISPUTE_DEADLINE_PASSED"
DISPUTE_ALREADY_SUBMITTED = "DISPUTE_ALREADY_SUBMITTED"
NO_EVIDENCE_TO_SUBMIT = "NO_EVIDENCE_TO_SUBMIT"
IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
INVALID_SIGNATURE = "INVALID_SIGNATURE"
VALIDATION_FAILED = "VALIDATION_FAILED"


def error_body(code: str, message: str, **extra) -> dict:
    return {"error": {"code": code, "message": message, **extra}}


async def handle_pb_error(_: Request, exc: ProofBridgeError):
    return JSONResponse(status_code=exc.status, content=error_body(exc.code, exc.message, **exc.extra))
