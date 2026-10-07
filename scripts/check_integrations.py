"""Contract spike (Guide §8.1): checks every real integration with your .env, read-only or test-only.
Never contests or accepts a dispute.

    python scripts/check_integrations.py pay_XXXXXXXX
"""
import io
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402

s = get_settings()
payment_id = sys.argv[1] if len(sys.argv) > 1 else None
results = []


def check(name):
    def wrap(fn):
        try:
            note = fn()
            results.append((True, name, note or ""))
            print(f"PASS  {name}  {note or ''}")
        except Exception as e:  # report and continue
            results.append((False, name, str(e)))
            print(f"FAIL  {name}  {e.__class__.__name__}: {e}")
        return fn
    return wrap


@check("database")
def _db():
    from sqlalchemy import inspect
    from app.db.session import engine
    tables = set(inspect(engine).get_table_names())
    need = {"dispute_runs", "evidence_items", "upload_sessions", "idempotency_keys"}
    missing = need - tables
    if missing:
        raise RuntimeError(f"missing tables {sorted(missing)}; run: alembic upgrade head")
    return engine.url.render_as_string(hide_password=True).split("@")[-1]


@check("mock disputes service")
def _mock():
    r = httpx.get(s.mock_disputes_base_url.rstrip("/") + "/v1/disputes?count=3",
                  auth=(s.razorpay_key_id, s.razorpay_key_secret), timeout=20)
    r.raise_for_status()
    return f"{r.json().get('count', 0)} disputes visible"


@check("razorpay fetch payment")
def _payment():
    if not payment_id:
        return "skipped (pass a pay_ id as the first argument)"
    from app.clients.razorpay import get_razorpay
    p = get_razorpay().fetch_payment(payment_id)
    return f"{p['id']} {p['status']} ₹{p['amount'] / 100:.2f} via {p.get('method')}"


@check("razorpay documents upload")
def _doc():
    from PIL import Image
    from app.clients.razorpay import get_razorpay
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (230, 230, 230)).save(buf, "JPEG")
    d = get_razorpay().upload_document("proofbridge_check.jpg", buf.getvalue(), "image/jpeg")
    return f"{d['id']} ({d.get('mime_type')})"


@check("storage")
def _storage():
    if s.storage_provider != "r2":
        return "local storage (set STORAGE_PROVIDER=r2 to test R2)"
    from app.clients.storage import get_storage
    st = get_storage()
    key = f"healthchecks/{uuid.uuid4()}.txt"
    url = st.presign_put(key, "text/plain")
    r = httpx.put(url, content=b"ok", headers={"Content-Type": "text/plain"}, timeout=20)
    r.raise_for_status()
    head = st.head(key)
    st.s3.delete_object(Bucket=st.bucket, Key=key)
    if not head:
        raise RuntimeError("uploaded via presigned URL but HEAD failed")
    return f"presigned PUT + HEAD ok in bucket {st.bucket}"


@check("ai extraction")
def _ai():
    from PIL import Image, ImageDraw
    from app.clients.ai import ExtractionInput, get_extractor
    im = Image.new("RGB", (900, 500), "white")
    d = ImageDraw.Draw(im)
    for i, line in enumerate(["Delhivery", "Shipment Delivered", "AWB: 1234567890123",
                              "Delivered on 2026-10-04 14:32", "Order PB-1001"]):
        d.text((40, 40 + i * 50), line, fill="black")
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    t = time.time()
    ex = get_extractor()
    out = ex.extract(ExtractionInput("synthetic_courier.jpg", "Customer says the order never arrived",
                                     images=[(buf.getvalue(), "image/jpeg")]))
    return f"[{ex.name} {getattr(ex, 'last_model_used', '') or ''}] {out.suggested_field} tracking={out.tracking_id} date={out.delivery_date} ({time.time() - t:.1f}s)"


ok = sum(r[0] for r in results)
print(f"\n{ok}/{len(results)} checks passed")
sys.exit(0 if ok == len(results) else 1)
