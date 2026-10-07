"""Evidence file storage. Originals never pass through the API in production:
the browser PUTs straight to a short-lived presigned R2 URL. Local mode mimics that with an API route."""
import hashlib
import hmac
import time
from pathlib import Path
from typing import Protocol

from app.config import get_settings


class Storage(Protocol):
    def presign_put(self, key: str, content_type: str) -> str: ...
    def presign_get(self, key: str) -> str: ...
    def head(self, key: str) -> dict | None: ...           # {"size": int, "content_type": str} or None
    def get_bytes(self, key: str) -> bytes: ...
    def put_bytes(self, key: str, data: bytes, content_type: str) -> None: ...


class R2Storage:
    def __init__(self, s):
        import boto3
        from botocore.config import Config
        self.bucket, self.expiry = s.r2_bucket, s.presign_expiry_seconds
        self.s3 = boto3.client("s3", endpoint_url=s.r2_endpoint, aws_access_key_id=s.r2_access_key_id,
                               aws_secret_access_key=s.r2_secret_access_key, region_name="auto",
                               config=Config(signature_version="s3v4"))

    def presign_put(self, key, content_type):
        return self.s3.generate_presigned_url("put_object", ExpiresIn=self.expiry,
                                              Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type})

    def presign_get(self, key):
        return self.s3.generate_presigned_url("get_object", ExpiresIn=self.expiry,
                                              Params={"Bucket": self.bucket, "Key": key})

    def head(self, key):
        from botocore.exceptions import ClientError
        try:
            r = self.s3.head_object(Bucket=self.bucket, Key=key)
            return {"size": r["ContentLength"], "content_type": r.get("ContentType", "")}
        except ClientError:
            return None

    def get_bytes(self, key):
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def put_bytes(self, key, data, content_type):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)


class LocalStorage:
    """Dev/test stand-in for R2. Signed URLs point at /api/v1/dev-storage on this API."""

    def __init__(self, s):
        self.root = Path(s.local_storage_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.base, self.secret, self.expiry = s.api_base_url.rstrip("/"), s.jwt_signing_secret.encode(), s.presign_expiry_seconds

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root)):
            raise ValueError("bad key")
        return p

    def sign(self, method: str, key: str, exp: int) -> str:
        return hmac.new(self.secret, f"{method}:{key}:{exp}".encode(), hashlib.sha256).hexdigest()

    def verify(self, method: str, key: str, exp: int, sig: str) -> bool:
        return exp > time.time() and hmac.compare_digest(self.sign(method, key, exp), sig)

    def _url(self, method, key):
        exp = int(time.time()) + self.expiry
        return f"{self.base}/api/v1/dev-storage/{key}?exp={exp}&sig={self.sign(method, key, exp)}"

    def presign_put(self, key, content_type):
        return self._url("PUT", key)

    def presign_get(self, key):
        return self._url("GET", key)

    def head(self, key):
        p = self._path(key)
        if not p.exists():
            return None
        ct = (p.with_suffix(p.suffix + ".ct").read_text() if p.with_suffix(p.suffix + ".ct").exists() else "")
        return {"size": p.stat().st_size, "content_type": ct}

    def get_bytes(self, key):
        return self._path(key).read_bytes()

    def put_bytes(self, key, data, content_type):
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        p.with_suffix(p.suffix + ".ct").write_text(content_type)


_storage = None


def get_storage():
    global _storage
    if _storage is None:
        s = get_settings()
        _storage = R2Storage(s) if s.storage_provider == "r2" else LocalStorage(s)
    return _storage
