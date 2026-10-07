import os
import sys
import tempfile
from pathlib import Path

import httpx
import pytest

_tmp = tempfile.mkdtemp(prefix="pb-test-")
os.environ.update({
    "APP_ENV": "test", "DATABASE_URL": f"sqlite:///{_tmp}/test.db", "STORAGE_PROVIDER": "local",
    "LOCAL_STORAGE_DIR": f"{_tmp}/uploads", "API_BASE_URL": "http://testserver", "AI_PROVIDER": "fake",
    "RAZORPAY_KEY_ID": "rzp_test_FAKE", "RAZORPAY_KEY_SECRET": "secret", "RAZORPAY_WEBHOOK_SECRET": "rzp_whsec",
    "MOCK_DISPUTES_WEBHOOK_SECRET": "mock_whsec", "DEMO_ADMIN_SECRET": "admin", "FRONTEND_ORIGIN": "http://localhost:5173",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.clients import disputes as disputes_mod  # noqa: E402
from app.clients import razorpay as rzp_mod  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.main import app  # noqa: E402
from tests.fakes import FakeMockDisputes, FakeRazorpay  # noqa: E402


@pytest.fixture()
def env():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    rzp = FakeRazorpay()
    mock = FakeMockDisputes(rzp)
    rzp_mod._client = rzp_mod.RazorpayClient("rzp_test_FAKE", "secret", "https://api.razorpay.com/v1",
                                             transport=httpx.MockTransport(rzp.handler))
    disputes_mod._provider = disputes_mod.MockDisputeProvider("https://mock.test", "rzp_test_FAKE", "secret",
                                                              transport=httpx.MockTransport(mock.handler))
    client = TestClient(app)
    yield client, rzp, mock


ADMIN = {"X-Demo-Secret": "admin"}


def seed_and_login(client, reason="goods_not_received", hours=72, payment_id="pay_TEST001"):
    r = client.post("/api/v1/demo/seed", headers=ADMIN,
                    json={"payment_id": payment_id, "reason_code": reason, "respond_in_hours": hours})
    assert r.status_code == 200, r.text
    seeded = r.json()
    r = client.post("/api/v1/sessions/exchange", json={"token": seeded["token"]})
    assert r.status_code == 200, r.text
    return seeded["dispute_id"], {"Authorization": f"Bearer {r.json()['access_token']}"}, seeded


def upload(client, did, auth, filename, data: bytes, mime: str):
    r = client.post(f"/api/v1/disputes/{did}/uploads", headers=auth,
                    json={"filename": filename, "mime_type": mime, "size_bytes": len(data)})
    assert r.status_code == 201, r.text
    up = r.json()
    put_path = up["put_url"].replace("http://testserver", "")
    r = client.put(put_path, content=data, headers={"Content-Type": mime})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/v1/disputes/{did}/uploads/{up['upload_id']}/complete", headers=auth, json={})
    assert r.status_code == 202, r.text
    eid = r.json()["evidence_id"]
    r = client.get(f"/api/v1/disputes/{did}/evidence/{eid}", headers=auth)   # background task already ran
    assert r.status_code == 200, r.text
    return r.json()
