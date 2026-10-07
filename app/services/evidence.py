"""Evidence pipeline (Guide §10–11) and evidence cards.

original in storage -> normalise (image/PDF/DOCX) -> AI extracts facts -> code checks facts against the
real payment + fixed rules -> if Strong/Relevant, upload the derivative to Razorpay Documents -> doc_ id.
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.clients.ai import ExtractionInput, get_extractor
from app.clients.razorpay import get_razorpay
from app.clients.storage import get_storage
from app.db.models import EvidenceItem, MerchantEdit
from app.db.session import SessionLocal
from app.domain.evidence_rules import ALL_FIELDS, FIELD_LABELS, Rule
from app.domain.matcher import NEEDS_ATTENTION, USEFUL, PaymentContext, evaluate
from app.errors import (FILE_PROCESSING_FAILED, NOT_FOUND, VALIDATION_FAILED, ProofBridgeError)
from app.services import files
from app.services.context import load_context, metric

log = logging.getLogger("proofbridge")

FACT_LABELS = {
    "tracking_id": "Tracking ID", "courier": "Courier", "delivery_status": "Delivery status",
    "delivery_date": "Delivered on", "amount_paise": "Amount", "order_id": "Order", "invoice_number": "Invoice",
    "customer_name": "Customer", "contact_last4": "Phone (last 4)", "customer_quote": "Customer wrote",
    "message_date": "Message date", "refund_reference": "Refund reference", "refund_date": "Refund date",
}
EDITABLE_FACTS = set(FACT_LABELS)
VERDICT_LABELS = {"strong": "Strong", "relevant": "Relevant", "needs_attention": "Needs attention",
                  "not_useful": "Not useful for this dispute"}
STAGES = ["queued", "extracting", "analysing", "validating", "uploading", "ready"]


def _now():
    return datetime.now(timezone.utc)


def get_item(db: Session, dispute_id: str, evidence_id: str) -> EvidenceItem:
    try:
        item = db.get(EvidenceItem, uuid.UUID(str(evidence_id)))
    except ValueError:
        item = None
    if item is None or item.dispute_id != dispute_id:
        raise ProofBridgeError(NOT_FOUND, "Evidence not found", 404)
    return item


# ---------- evaluation + Razorpay upload ----------

def apply_evaluation(item: EvidenceItem, rule: Rule, ctx: PaymentContext) -> None:
    ev = evaluate(item.final_field, item.final_facts or {}, item.readable or item.merchant_confirmed,
                  item.quality_flags or [], rule, ctx)
    if ev.verdict == NEEDS_ATTENTION and item.merchant_confirmed and ev.mismatches:
        # seller confirmed it belongs: keep the mismatches visible, but count the file by its field role
        role = rule.role(item.final_field)
        item.verdict = "strong" if role == "required" else "relevant"
        item.verdict_reason = "You confirmed this belongs to the order. " + ev.mismatches[0]["message"]
    else:
        item.verdict, item.verdict_reason = ev.verdict, ev.reason
    if item.bank_flag:
        item.verdict, item.verdict_reason = NEEDS_ATTENTION, f"The bank asked for clearer proof: {item.bank_flag}"
    item.mismatches = ev.mismatches
    item.processing_notes = [n for n in (item.processing_notes or []) if not n.startswith("matched:")] + \
        ([f"matched:{','.join(ev.matched)}"] if ev.matched else [])


def upload_to_razorpay_if_useful(item: EvidenceItem) -> None:
    if item.verdict not in USEFUL or item.razorpay_document_id or not item.normalized_object_key:
        return
    data = get_storage().get_bytes(item.normalized_object_key)
    mime = files.PDF if item.normalized_object_key.endswith(".pdf") else files.JPEG
    name = (item.original_filename.rsplit(".", 1)[0] or "evidence")[:60] + (".pdf" if mime == files.PDF else ".jpg")
    doc = get_razorpay().upload_document(name, data, mime)
    item.razorpay_document_id = doc["id"]


# ---------- full processing (runs as a background task) ----------

def process_evidence(evidence_id: str, force: bool = False) -> None:
    db = SessionLocal()
    try:
        item = db.get(EvidenceItem, uuid.UUID(str(evidence_id)))
        if item is None:
            return
        _process(db, item, force)
    finally:
        db.close()


def _set(db, item, status):
    item.status = status
    db.commit()


def _process(db: Session, item: EvidenceItem, force: bool) -> None:
    item.error_code = None
    started = _now()
    try:
        dctx = load_context(item.dispute_id)
        needs_extraction = force or not item.suggested_field
        if needs_extraction:
            _set(db, item, "extracting")
            original = get_storage().get_bytes(item.original_object_key)
            norm = files.normalize(original, item.original_filename)
            item.detected_mime_type = norm.detected_mime
            key = item.original_object_key.rsplit("/", 1)[0] + "/normalized" + (
                ".pdf" if norm.rzp_mime == files.PDF else ".jpg")
            get_storage().put_bytes(key, norm.rzp_bytes, norm.rzp_mime)
            item.normalized_object_key = key
            item.processing_notes = list(norm.notes)

            _set(db, item, "analysing")
            ai = get_extractor().extract(ExtractionInput(
                filename=item.original_filename, dispute_reason=dctx.rule.reason_label,
                images=norm.ai_images, text=norm.ai_text))
            facts = {k: v for k, v in ai.model_dump().items()
                     if k in EDITABLE_FACTS and v not in (None, "", [])}
            item.document_type = ai.document_type
            item.suggested_field = ai.suggested_field if ai.suggested_field in ALL_FIELDS else "none"
            item.final_field = item.final_field if (item.final_field and not force) else item.suggested_field
            item.readable = ai.readable
            item.quality_flags = ai.quality_issues
            item.original_facts = facts
            item.final_facts = dict(facts) if (force or not item.final_facts) else item.final_facts
            item.question = None
            if ai.field_confidence == "low" and item.suggested_field != "none":
                order = dctx.ctx.order_refs and sorted(dctx.ctx.order_refs)[0]
                item.question = (f"Is this the {FIELD_LABELS[item.suggested_field].lower()} for "
                                 f"{('order ' + order) if order else 'this payment'}?")

        _set(db, item, "validating")
        apply_evaluation(item, dctx.rule, dctx.ctx)

        if item.verdict in USEFUL and not item.razorpay_document_id:
            _set(db, item, "uploading")
            try:
                upload_to_razorpay_if_useful(item)
            except ProofBridgeError as e:   # keep the analysis; the file can be retried alone
                item.error_code = e.code
                item.processing_notes = [*(item.processing_notes or []), e.message]
        item.status = "ready"
        db.commit()
        metric(db, "evidence_processed", item.dispute_id, item.id, verdict=item.verdict, field=item.final_field,
               seconds=round((_now() - started).total_seconds(), 2), ai=get_extractor().name)
    except ProofBridgeError as e:
        db.rollback()
        item.status, item.error_code = "failed", e.code
        item.processing_notes = [*(item.processing_notes or []), e.message]
        db.commit()
        metric(db, "evidence_failed", item.dispute_id, item.id, code=e.code)
    except Exception as e:   # never leave a card spinning
        log.exception("processing failed")
        db.rollback()
        item.status, item.error_code = "failed", FILE_PROCESSING_FAILED
        item.processing_notes = [*(item.processing_notes or []), f"Unexpected error: {e.__class__.__name__}"]
        db.commit()


# ---------- seller actions ----------

def correct(db: Session, item: EvidenceItem, body: dict) -> EvidenceItem:
    """body keys (any combination): fact_key+value, final_field, confirm (bool), answer ('yes'|'no')."""
    changed = False
    if "fact_key" in body:
        k = body["fact_key"]
        if k not in EDITABLE_FACTS:
            raise ProofBridgeError(VALIDATION_FAILED, f"fact_key must be one of {sorted(EDITABLE_FACTS)}", 422)
        new = body.get("value")
        if k == "amount_paise" and new is not None:
            try:
                new = int(new)
            except (TypeError, ValueError):
                raise ProofBridgeError(VALIDATION_FAILED, "amount_paise must be an integer (paise)", 422)
        old = (item.final_facts or {}).get(k)
        if old != new:
            facts = dict(item.final_facts or {})
            if new in (None, ""):
                facts.pop(k, None)
            else:
                facts[k] = new
            item.final_facts = facts
            item.correction_count += 1
            db.add(MerchantEdit(evidence_id=item.id, fact_key=k, old_value=old, new_value=new))
            metric(db, "fact_corrected", item.dispute_id, item.id, fact=k)
            changed = True
    if "final_field" in body:
        f = body["final_field"]
        if f not in ALL_FIELDS and f != "none":
            raise ProofBridgeError(VALIDATION_FAILED, "final_field must be a Razorpay evidence field or 'none'", 422)
        if f != item.final_field:
            db.add(MerchantEdit(evidence_id=item.id, fact_key="_field", old_value=item.final_field, new_value=f))
            item.final_field, item.correction_count = f, item.correction_count + 1
            # same file, so an existing doc_ id stays valid under the new field
            changed = True
    if body.get("confirm"):
        item.merchant_confirmed, changed = True, True
    if "answer" in body:
        if body["answer"] == "no":
            item.final_field = "none"
        item.question, changed = None, True
    if body.get("clear_bank_flag"):
        item.bank_flag, changed = None, True
    if changed:
        dctx = load_context(item.dispute_id)
        apply_evaluation(item, dctx.rule, dctx.ctx)
        try:
            upload_to_razorpay_if_useful(item)
            item.error_code = None if item.error_code == "RAZORPAY_DOCUMENT_UPLOAD_FAILED" else item.error_code
        except ProofBridgeError as e:
            item.error_code = e.code
        db.commit()
    return item


def remove(db: Session, item: EvidenceItem) -> None:
    item.removed_at = _now()          # audit row kept; excluded from coverage and submission
    db.commit()
    metric(db, "evidence_removed", item.dispute_id, item.id)


# ---------- serialisation ----------

def card(item: EvidenceItem, rule: Rule | None = None, with_preview: bool = True) -> dict:
    facts = item.final_facts or {}
    orig = item.original_facts or {}
    matched = next((n.split(":", 1)[1].split(",") for n in (item.processing_notes or []) if n.startswith("matched:")), [])
    fact_rows = []
    for k, label in FACT_LABELS.items():
        if k not in facts:
            continue
        v = facts[k]
        shown = f"₹{int(v) / 100:,.2f}".replace(".00", "") if k == "amount_paise" else v
        fact_rows.append({"key": k, "label": label, "value": v, "display": shown,
                          "source": item.original_filename,
                          "edited": orig.get(k) != v, "matches_payment": k in matched})
    preview = None
    if with_preview and item.normalized_object_key:
        try:
            preview = get_storage().presign_get(item.normalized_object_key)
        except Exception:
            preview = None
    return {
        "id": str(item.id), "status": item.status,
        "stage_index": STAGES.index(item.status) if item.status in STAGES else None,
        "error": {"code": item.error_code, "message": (item.processing_notes or [""])[-1]} if item.error_code else None,
        "filename": item.original_filename, "document_type": item.document_type,
        "detected_mime_type": item.detected_mime_type,
        "field": item.final_field, "field_label": FIELD_LABELS.get(item.final_field or "", "Not used"),
        "suggested_field": item.suggested_field,
        "role": rule.role(item.final_field) if rule else None,
        "verdict": item.verdict, "verdict_label": VERDICT_LABELS.get(item.verdict or ""),
        "reason": item.verdict_reason, "question": item.question,
        "facts": fact_rows, "mismatches": item.mismatches or [], "quality_issues": item.quality_flags or [],
        "readable": item.readable, "merchant_confirmed": item.merchant_confirmed, "bank_flag": item.bank_flag,
        "razorpay_document_id": item.razorpay_document_id, "correction_count": item.correction_count,
        "notes": [n for n in (item.processing_notes or []) if not n.startswith("matched:")],
        "preview_url": preview, "removed": item.removed_at is not None,
    }
