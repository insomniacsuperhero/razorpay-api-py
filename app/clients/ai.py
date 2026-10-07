"""AI extraction. The model only reads files and suggests an evidence field.
It never decides what is required, the verdict, or whether to submit (that is app/domain).

Providers:
  gemini - Gemini Flash, structured JSON output validated against EvidenceAI
  fake   - deterministic regex/keyword extractor for local dev, tests and the labelled demo fallback
Use synthetic or anonymised evidence on the Gemini free tier (free-tier content may be used by Google).
"""
import json
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.config import get_settings
from app.domain.evidence_rules import ALL_FIELDS
from app.errors import AI_ANALYSIS_FAILED, ProofBridgeError

FieldName = Literal[
    "shipping_proof", "billing_proof", "cancellation_proof", "customer_communication", "proof_of_service",
    "explanation_letter", "refund_confirmation", "access_activity_log", "refund_cancellation_policy",
    "term_and_conditions", "others", "none"]


class EvidenceAI(BaseModel):
    document_type: str = Field(description="Short plain label, e.g. 'Courier tracking screenshot', 'WhatsApp chat'")
    suggested_field: FieldName = Field(description="Razorpay evidence field this file best fits, or 'none'")
    field_confidence: Literal["high", "low"] = "high"
    readable: bool = Field(description="False if blurry, cropped or key text cannot be read")
    quality_issues: list[str] = Field(default_factory=list, description="e.g. 'delivery date cut off'")
    tracking_id: str | None = None
    courier: str | None = None
    delivery_status: str | None = None
    delivery_date: str | None = Field(None, description="ISO YYYY-MM-DD if visible")
    amount_paise: int | None = Field(None, description="Amount shown, in paise (₹2,499 -> 249900)")
    order_id: str | None = None
    invoice_number: str | None = None
    customer_name: str | None = None
    contact_last4: str | None = Field(None, description="Last 4 digits of a phone number shown")
    customer_quote: str | None = Field(None, description="Customer's own words confirming receipt etc., max 200 chars")
    message_date: str | None = Field(None, description="ISO date of the quoted message if visible")
    refund_reference: str | None = None
    refund_date: str | None = None


@dataclass
class ExtractionInput:
    filename: str
    dispute_reason: str
    images: list[tuple[bytes, str]] = field(default_factory=list)   # (bytes, mime) e.g. page renders
    text: str | None = None


PROMPT = """You are reading ONE file a merchant selected as evidence for a payment dispute.
Dispute reason: {reason}.
Extract only facts that are visibly present. Never guess or invent values; use null when not visible.
Pick suggested_field from: {fields}, or 'none' if the file does not support any.
Field guide: shipping_proof = courier/delivery proof; customer_communication = chats/emails with the customer;
billing_proof = invoice/receipt; proof_of_service = product/packing photos or service completion;
refund_confirmation = refund receipt; cancellation_proof = cancellation record;
refund_cancellation_policy = policy text; others = anything relevant that fits nothing else.
Set field_confidence to 'low' if unsure which field fits. Set readable=false if key text is unreadable.
Amounts in paise (₹2,499 = 249900). Dates as YYYY-MM-DD.{text_block}"""


class GeminiExtractor:
    name = "gemini"

    CALL_TIMEOUT_MS = 30_000     # one Gemini request
    TOTAL_BUDGET_S = 75          # all retries/fallbacks for one file; keeps a card from spinning forever
    PRIMARY_SHARE_S = 35         # the main model must answer within this, else the fallback takes over

    def __init__(self, api_key: str, model: str, fallback_model: str = ""):
        from google import genai
        from google.genai import types
        self._genai = genai
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=self.CALL_TIMEOUT_MS))
        self.model = model
        self.fallback_model = fallback_model
        self.last_model_used: str | None = None

    @staticmethod
    def _describe(e: Exception) -> str:
        code = getattr(e, "code", None) or getattr(e, "status_code", None)
        msg = str(getattr(e, "message", None) or e).replace("\n", " ")
        return f"{e.__class__.__name__}{f' {code}' if code else ''}: {msg[:300]}"

    @staticmethod
    def _retryable(e: Exception) -> bool:
        code = getattr(e, "code", None)
        return e.__class__.__name__ == "ServerError" or code in (429, 500, 502, 503, 504) or isinstance(
            e, (TimeoutError, ConnectionError))

    def _config(self, mode: str):
        from google.genai import types
        common = dict(response_mime_type="application/json", temperature=0,
                      automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        if mode == "schema":            # SDK converts the Pydantic model
            return types.GenerateContentConfig(response_schema=EvidenceAI, **common)
        if mode == "json_schema":       # plain JSON Schema; some models accept this when the above fails
            return types.GenerateContentConfig(response_json_schema=EvidenceAI.model_json_schema(), **common)
        return types.GenerateContentConfig(**common)   # JSON mode only; schema is in the prompt, Pydantic validates

    def extract(self, inp: ExtractionInput) -> EvidenceAI:
        import logging
        import time
        from google.genai import types
        log = logging.getLogger("proofbridge.ai")
        text_block = f"\n\nExtracted document text (may be partial):\n{inp.text[:12000]}" if inp.text else ""
        prompt = PROMPT.format(reason=inp.dispute_reason, fields=", ".join(ALL_FIELDS), text_block=text_block)
        images = [types.Part.from_bytes(data=b, mime_type=m) for b, m in inp.images[:5]]
        schema_hint = "\n\nReturn ONLY a JSON object matching this JSON Schema:\n" + json.dumps(EvidenceAI.model_json_schema())

        last_err = "no attempt made"
        started = time.monotonic()
        models = [m for m in (self.model, self.fallback_model) if m]
        for idx, model in enumerate(models):
            is_last = idx == len(models) - 1
            # the main model gets a capped share of the budget so the fallback always gets a turn
            model_deadline = started + (self.TOTAL_BUDGET_S if is_last else self.PRIMARY_SHARE_S)
            next_model = False
            for mode in ("schema", "json_schema", "prompt"):
                if next_model:
                    break
                contents = images + [prompt + (schema_hint if mode == "prompt" else "")]
                for attempt in range(2):          # one quick retry for a transient 5xx / 429
                    now = time.monotonic()
                    if now - started > self.TOTAL_BUDGET_S:
                        raise ProofBridgeError(AI_ANALYSIS_FAILED, f"AI analysis timed out; last error: {last_err}", 504)
                    if now > model_deadline:
                        next_model = True
                        break
                    try:
                        res = self.client.models.generate_content(model=model, contents=contents,
                                                                  config=self._config(mode))
                    except Exception as e:
                        last_err = self._describe(e)
                        code = getattr(e, "code", None)
                        log.warning("gemini %s mode=%s attempt=%d failed: %s", model, mode, attempt + 1, last_err)
                        if code in (401, 403):                 # bad key: nothing else will work
                            raise ProofBridgeError(AI_ANALYSIS_FAILED, f"AI analysis failed: {last_err}", 502)
                        if self._retryable(e):
                            if attempt == 0 and not (not is_last and time.monotonic() + 1.5 > model_deadline):
                                time.sleep(1.5)
                                continue
                            next_model = True                  # overloaded / rate limited: go to the fallback
                        elif code == 404:
                            next_model = True                  # model retired or unknown: go to the fallback
                        break                                  # otherwise (e.g. 400 schema rejected): next mode
                    try:
                        out = res.parsed if isinstance(res.parsed, EvidenceAI) else \
                            EvidenceAI.model_validate(json.loads(res.text or "{}"))
                        self.last_model_used = f"{model} ({mode})"
                        if idx > 0:
                            log.info("gemini fallback %s answered after %s failed", model, self.model)
                        return out
                    except (ValidationError, ValueError) as e:
                        last_err = f"output failed validation: {str(e)[:200]}"
                        log.warning("gemini %s mode=%s returned invalid JSON: %s", model, mode, last_err)
                        break
        raise ProofBridgeError(AI_ANALYSIS_FAILED, f"AI analysis failed: {last_err}", 502)


class FakeExtractor:
    """Deterministic stand-in. Reads text (PDF/DOCX) with regexes and uses filename keywords for images.
    Only for local development, tests and an explicitly labelled demo fallback."""
    name = "fake"
    _rx = {
        "tracking_id": r"\b(?:AWB|Tracking(?: ID)?|Docket)[:#\s]*([A-Z0-9]{8,20})\b",
        "order_id": r"\bOrder(?: ID| No\.?)?[:#\s]*([A-Z0-9-]{4,24})\b",
        "invoice_number": r"\bInvoice(?: No\.?| #)?[:#\s]*([A-Z0-9/-]{3,24})\b",
        "delivery_date": r"\bDelivered(?: on)?[:\s]*(\d{4}-\d{2}-\d{2})\b",
        "refund_reference": r"\b(?:Refund (?:ID|Ref(?:erence)?))[:#\s]*([A-Za-z0-9_]{6,30})\b",
    }

    def extract(self, inp: ExtractionInput) -> EvidenceAI:
        name, text = inp.filename.lower(), inp.text or ""
        facts: dict = {k: (m.group(1) if (m := re.search(rx, text, re.I)) else None) for k, rx in self._rx.items()}
        amt = re.search(r"(?:₹|Rs\.?|INR)\s?([\d,]+(?:\.\d{1,2})?)", text)
        if amt:
            facts["amount_paise"] = round(float(amt.group(1).replace(",", "")) * 100)
        courier = re.search(r"\b(Delhivery|Blue ?Dart|DTDC|Ekart|Xpressbees|Shadowfax|India Post|Ecom Express)\b", text, re.I)
        facts["courier"] = courier.group(1) if courier else None
        quote = re.search(r"[\"“]([^\"”]{4,200})[\"”]", text)
        facts["customer_quote"] = quote.group(1) if quote else None

        def has(*words):
            return any(w in name for w in words) or any(w in text.lower() for w in words)

        if has("courier", "delivery", "delivered", "tracking", "awb", "shipment"):
            fld, label = "shipping_proof", "Courier tracking proof"
            facts["delivery_status"] = facts.get("delivery_status") or "Delivered"
            if not text and not facts["tracking_id"]:      # image with a telling filename (demo)
                facts.update(tracking_id="DL78421135", courier=facts["courier"] or "Delhivery")
        elif has("whatsapp", "chat", "message", "received, thank"):
            fld, label = "customer_communication", "Chat with the customer"
            if not facts["customer_quote"] and not text:
                facts["customer_quote"] = "Received, thank you!"
        elif has("invoice", "bill", "receipt"):
            fld, label = "billing_proof", "Invoice"
        elif has("refund"):
            fld, label = "refund_confirmation", "Refund confirmation"
        elif has("policy", "terms"):
            fld, label = "refund_cancellation_policy", "Store policy"
        elif has("packing", "product", "photo", "item"):
            fld, label = "proof_of_service", "Product or packing photo"
        else:
            fld, label = "none", "Unrecognised file"
        unreadable = has("blurry", "blur", "unreadable")
        return EvidenceAI(document_type=label, suggested_field=fld, readable=not unreadable,
                          field_confidence="low" if fld == "none" else "high",
                          quality_issues=["Image is blurry; key text can't be read"] if unreadable else [],
                          **{k: v for k, v in facts.items() if v is not None})


_extractor = None


def get_extractor():
    global _extractor
    if _extractor is None:
        s = get_settings()
        _extractor = GeminiExtractor(s.gemini_api_key, s.gemini_model, s.gemini_fallback_model) if s.ai_provider == "gemini" else FakeExtractor()
    return _extractor
