"""Unit tests for the deterministic parts: rules, matcher, summary, file handling."""
import io
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.domain.evidence_rules import DISPUTE_RULES, rule_for
from app.domain.matcher import PaymentContext, evaluate
from app.domain.summary import build_summary
from app.errors import ProofBridgeError
from app.services import files
from tests import samples

CTX = PaymentContext(payment_id="pay_X", amount=249900, disputed_amount=249900,
                     created_at=datetime.now(timezone.utc) - timedelta(days=5), contact_last4="0001",
                     order_refs={"PB1001"})
NR = DISPUTE_RULES["goods_not_received"]


def test_required_field_with_key_fact_is_strong():
    assert evaluate("shipping_proof", {"tracking_id": "AWB1"}, True, [], NR, CTX).verdict == "strong"


def test_required_field_without_key_fact_is_relevant():
    assert evaluate("shipping_proof", {"courier": "DTDC"}, True, [], NR, CTX).verdict == "relevant"


def test_helpful_field_is_relevant_and_unlisted_is_not_useful():
    assert evaluate("billing_proof", {}, True, [], NR, CTX).verdict == "relevant"
    assert evaluate("proof_of_service", {}, True, [], NR, CTX).verdict == "not_useful"


def test_mismatches_amount_phone_order_and_date():
    for facts in ({"amount_paise": 1}, {"contact_last4": "9999"}, {"order_id": "PB-9999"},
                  {"delivery_date": "2001-01-01"}):
        ev = evaluate("shipping_proof", {"tracking_id": "A", **facts}, True, [], NR, CTX)
        assert ev.verdict == "needs_attention", facts
    ok = evaluate("shipping_proof", {"tracking_id": "A", "order_id": "pb-1001", "amount_paise": 249900}, True, [], NR, CTX)
    assert ok.verdict == "strong" and set(ok.matched) == {"order_id", "amount_paise"}


def test_unreadable_is_needs_attention():
    assert evaluate("shipping_proof", {"tracking_id": "A"}, False, ["date cut off"], NR, CTX).verdict == "needs_attention"


def test_unknown_reason_requires_nothing():
    r = rule_for("network_code_4855")
    assert r.required == () and "shipping_proof" in r.helpful


def test_summary_only_uses_card_facts_and_fits_limit():
    items = [SimpleNamespace(verdict="strong", removed_at=None, document_type="Courier tracking screenshot",
                             final_facts={"tracking_id": "DL1", "courier": "Delhivery", "delivery_status": "Delivered",
                                          "customer_quote": "x" * 900}),
             SimpleNamespace(verdict="not_useful", removed_at=None, document_type="Photo",
                             final_facts={"invoice_number": "SHOULD-NOT-APPEAR"})]
    s = build_summary(CTX, NR.reason_label, items)
    assert "DL1" in s and "SHOULD-NOT-APPEAR" not in s and len(s) <= 1000


def test_detect_mime_by_magic_bytes():
    assert files.detect_mime(samples.jpeg()[:16]) == files.JPEG
    assert files.detect_mime(samples.png()[:16]) == files.PNG
    assert files.detect_mime(samples.pdf(["x"])[:16]) == files.PDF
    assert files.detect_mime(b"MZ\x90\x00") is None


def test_image_normalised_to_jpeg_without_exif():
    n = files.normalize(samples.png(), "screenshot.png")
    assert n.rzp_mime == files.JPEG and n.rzp_bytes[:3] == b"\xff\xd8\xff" and n.ai_images


def test_large_pdf_sends_only_relevant_pages_to_ai():
    data = samples.pdf(["Invoice No: INV-1", "Order: PB-1001", "Total: Rs. 2,499", "Delivered by Delhivery AWB 123456789"],
                       pages=12)
    n = files.normalize(data, "statement.pdf")
    assert len(n.ai_images) == files.PDF_AI_PAGES
    assert "[page 11]" in (n.ai_text or "")     # the page with the facts was picked


def test_docx_zip_bomb_and_macros_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "<w/>")
        z.writestr("word/vbaProject.bin", "x")
    with pytest.raises(ProofBridgeError):
        files.normalize(buf.getvalue(), "evil.docm")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "0" * (files.DOCX_MAX_EXPANDED + 10))
    with pytest.raises(ProofBridgeError):
        files.normalize(buf.getvalue(), "bomb.docx")


def test_heic_gets_a_helpful_message():
    with pytest.raises(ProofBridgeError) as e:
        files.normalize(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20, "IMG_0001.HEIC")
    assert "HEIC" in e.value.message


def test_gemini_extractor_parses_and_fails_safely(monkeypatch):
    import time
    monkeypatch.setattr(time, 'sleep', lambda s: None)
    from app.clients.ai import EvidenceAI, ExtractionInput, GeminiExtractor

    class Res:
        parsed = None
        text = '{"document_type":"Courier screenshot","suggested_field":"shipping_proof","readable":true,"tracking_id":"AWB9"}'

    g = GeminiExtractor("test-key", "gemini-test")
    g.client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: Res()))
    out = g.extract(ExtractionInput("a.jpg", "Customer says the order never arrived", images=[(b"\xff\xd8\xff", "image/jpeg")]))
    assert isinstance(out, EvidenceAI) and out.tracking_id == "AWB9"

    def boom(**kw):
        raise TimeoutError("rate limited")
    g.client = SimpleNamespace(models=SimpleNamespace(generate_content=boom))
    with pytest.raises(ProofBridgeError) as e:
        g.extract(ExtractionInput("a.jpg", "x"))
    assert e.value.code == "AI_ANALYSIS_FAILED"

    Res.text = '{"suggested_field":"made_up_field"}'
    g.client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: Res()))
    with pytest.raises(ProofBridgeError):
        g.extract(ExtractionInput("a.jpg", "x"))


def test_ai_failure_marks_card_failed_and_retry_works(env):
    from app.clients import ai as ai_mod
    from tests.conftest import seed_and_login, upload
    client, *_ = env
    did, auth, _ = seed_and_login(client)
    real = ai_mod._extractor

    class Down:
        name = "down"
        def extract(self, inp):
            raise ProofBridgeError("AI_ANALYSIS_FAILED", "AI analysis failed: rate limited", 502)
    ai_mod._extractor = Down()
    try:
        card = upload(client, did, auth, "delhivery_tracking.jpg", samples.jpeg(), "image/jpeg")
        assert card["status"] == "failed" and card["error"]["code"] == "AI_ANALYSIS_FAILED"
    finally:
        ai_mod._extractor = real
    r = client.post(f"/api/v1/disputes/{did}/evidence/{card['id']}/process", headers=auth)
    assert r.status_code == 202
    card = client.get(f"/api/v1/disputes/{did}/evidence/{card['id']}", headers=auth).json()
    assert card["status"] == "ready" and card["verdict"] == "strong"


def test_gemini_falls_back_to_second_model(monkeypatch):
    import time
    from app.clients.ai import ExtractionInput, GeminiExtractor
    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls = []

    class ServerError(Exception):
        code = 503

    class Res:
        parsed = None
        text = '{"document_type":"Courier screenshot","suggested_field":"shipping_proof","readable":true}'

    def gen(model, contents, config):
        calls.append(model)
        if model == "main-model":
            raise ServerError("overloaded")
        return Res()
    g = GeminiExtractor("k", "main-model", "backup-model")
    g.client = SimpleNamespace(models=SimpleNamespace(generate_content=gen))
    out = g.extract(ExtractionInput("a.jpg", "x"))
    assert out.suggested_field == "shipping_proof" and calls[-1] == "backup-model"
    assert g.last_model_used.startswith("backup-model")
