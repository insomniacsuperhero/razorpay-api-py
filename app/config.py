"""Settings. Every secret is server-side; the browser only ever sees signed URLs and tokens.

Local development runs with no external accounts:
  STORAGE_PROVIDER=local  (files saved under ./.uploads, uploaded through the API)
  AI_PROVIDER=fake        (deterministic demo extractor; clearly labelled as fallback)
  DATABASE_URL=sqlite:///./dev.db
Production (Render) sets STORAGE_PROVIDER=r2, AI_PROVIDER=gemini and a Supabase DATABASE_URL.
"""
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    app_env: str = "development"
    frontend_origin: str = "http://localhost:5173"
    extra_cors_origins: str = ""            # comma-separated, e.g. a Vercel preview URL
    api_base_url: str = "http://localhost:8000"
    jwt_signing_secret: str = "dev-only-change-me-dev-only-change-me"
    demo_admin_secret: str = "dev-admin-secret"
    access_token_minutes: int = 120
    link_token_minutes: int = 30

    # Database (Supabase session pooler URL in production)
    database_url: str = "sqlite:///./dev.db"
    auto_create_tables: bool = False         # dev convenience; use Alembic in production

    # Storage
    storage_provider: str = "local"          # local | r2
    local_storage_dir: str = "./.uploads"
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = "proof-bridge-evidence"
    r2_endpoint: str = ""
    presign_expiry_seconds: int = 900
    max_upload_bytes: int = 100 * 1024 * 1024

    # Razorpay test mode
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    razorpay_base_url: str = "https://api.razorpay.com/v1"

    # Disputes provider
    disputes_provider: str = "mock"          # mock | razorpay
    mock_disputes_base_url: str = "https://razorpay-mock-disputes.vercel.app"
    mock_disputes_webhook_secret: str = ""

    # AI
    ai_provider: str = "fake"                # gemini | fake
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.7-flash"   # set to the current Flash model in your account
    gemini_fallback_model: str = ""           # optional, e.g. gemini-3.5-flash-lite; used if the main model errors

    # Merchant shown in the app (the standard Razorpay API has no merchant-profile endpoint)
    merchant_id: str = "merchant_demo"
    merchant_name: str = "Your business"
    # Prototype only: let anyone who opens the app use this test merchant without an alert link
    demo_open_access: bool = False

    # Dispute nudges: who gets them and through which channels
    notify_channels: str = "in_app,push,email,whatsapp"   # any of in_app, push, email, whatsapp
    alert_link_minutes: int = 1440                   # alert links stay valid 24 h (still single use)
    reminder_hours: str = "24,6"                     # reminders before the deadline if no response yet
    merchant_email: str = ""
    merchant_whatsapp: str = ""                      # E.164 digits, e.g. 919812345678
    resend_api_key: str = ""
    notify_email_from: str = "Razorpay Disputes <onboarding@resend.dev>"
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_template: str = ""                      # approved template; empty = free-form text (24 h window)
    whatsapp_template_lang: str = "en"
    # Web Push to the installed app (generate once: python scripts/vapid_keys.py)
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:disputes@example.com"

    @property
    def cors_origins(self) -> list[str]:
        extra = [o.strip() for o in self.extra_cors_origins.split(",") if o.strip()]
        return [self.frontend_origin, *extra]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @model_validator(mode="after")
    def _check_production(self):
        # An empty value in .env must not silently weaken signing (any environment)
        if not self.jwt_signing_secret:
            raise ValueError("JWT_SIGNING_SECRET is empty. Generate one: "
                             "python -c \"import secrets;print(secrets.token_urlsafe(48))\"")
        if self.app_env != "production":
            return self
        missing = []
        if self.jwt_signing_secret.startswith("dev-only") or len(self.jwt_signing_secret) < 32:
            missing.append("JWT_SIGNING_SECRET (32+ random bytes)")
        if self.demo_admin_secret in ("", "dev-admin-secret"):
            missing.append("DEMO_ADMIN_SECRET")
        if self.database_url.startswith("sqlite"):
            missing.append("DATABASE_URL (Supabase)")
        if not self.razorpay_key_id.startswith("rzp_test_"):
            missing.append("RAZORPAY_KEY_ID (must be a rzp_test_ key)")
        if not self.razorpay_key_secret:
            missing.append("RAZORPAY_KEY_SECRET")
        if self.storage_provider == "r2" and not all(
            [self.r2_access_key_id, self.r2_secret_access_key, self.r2_endpoint]):
            missing.append("R2_* credentials")
        if self.ai_provider == "gemini" and not self.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        if missing:
            raise ValueError("Missing production settings: " + ", ".join(missing))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
