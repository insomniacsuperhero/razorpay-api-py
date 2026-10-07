import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.routes import router
from app.config import get_settings
from app.errors import VALIDATION_FAILED, ProofBridgeError, error_body, handle_pb_error

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
settings = get_settings()

app = FastAPI(title="Proof Bridge API", version="0.1.0",
              description="Phone evidence → AI facts → rule-checked → real Razorpay doc_ IDs → contest via provider.")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                   allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
                   allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
                   expose_headers=["Idempotent-Replay"])
app.add_exception_handler(ProofBridgeError, handle_pb_error)


@app.exception_handler(RequestValidationError)
async def _validation(_: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
    return JSONResponse(status_code=422, content=error_body(VALIDATION_FAILED, f"{loc}: {first.get('msg', 'invalid')}"))


app.include_router(router, prefix="/api/v1")

if settings.auto_create_tables:
    from app.db.models import Base
    from app.db.session import engine
    Base.metadata.create_all(engine)


@app.get("/health", tags=["health"])
def health():
    return {"ok": True, "service": "proof-bridge-api", "env": settings.app_env,
            "storage": settings.storage_provider, "ai": settings.ai_provider,
            "disputes": settings.disputes_provider,
            "razorpay_configured": bool(settings.razorpay_key_id and settings.razorpay_key_secret)}
