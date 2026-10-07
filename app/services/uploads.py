"""Direct-to-storage uploads (Guide §5, §9.1–9.2). Bytes never pass through the API in production."""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.clients.storage import get_storage
from app.config import get_settings
from app.db.models import EvidenceItem, UploadSession
from app.errors import (FILE_TOO_LARGE, NOT_FOUND, R2_UPLOAD_MISSING, UNSUPPORTED_FILE_TYPE,
                        VALIDATION_FAILED, ProofBridgeError)
from app.services import files
from app.services.context import ensure_run, metric

EXT = {files.JPEG: ".jpg", "image/jpg": ".jpg", files.PNG: ".png", files.PDF: ".pdf", files.DOCX: ".docx",
       files.HEIC: ".heic", "image/heif": ".heic"}


def create_upload(db: Session, dispute: dict, filename: str, mime_type: str, size_bytes: int) -> dict:
    s = get_settings()
    mime_type = (mime_type or "").lower()
    if mime_type not in files.ACCEPTED_DECLARED:
        raise ProofBridgeError(UNSUPPORTED_FILE_TYPE, "Only JPG, PNG, PDF and Word (.docx) files are supported.", 415)
    if size_bytes <= 0:
        raise ProofBridgeError(VALIDATION_FAILED, "size_bytes must be positive", 422)
    if size_bytes > s.max_upload_bytes:
        raise ProofBridgeError(FILE_TOO_LARGE, f"Files up to {s.max_upload_bytes // (1024 * 1024)} MB are supported.", 413)
    ensure_run(db, dispute)
    up_id = uuid.uuid4()
    key = f"disputes/{dispute['id']}/{up_id}/original{EXT[mime_type]}"   # random key; never the user's path
    db.add(UploadSession(id=up_id, dispute_id=dispute["id"], object_key=key,
                         original_filename=(filename or "evidence")[:200], declared_mime_type=mime_type,
                         size_bytes=size_bytes, status="pending",
                         expires_at=datetime.now(timezone.utc) + timedelta(seconds=s.presign_expiry_seconds)))
    db.commit()
    metric(db, "upload_created", dispute["id"], mime=mime_type, size=size_bytes)
    return {"upload_id": str(up_id), "object_key": key, "put_url": get_storage().presign_put(key, mime_type),
            "method": "PUT", "headers": {"Content-Type": mime_type}, "expires_in": s.presign_expiry_seconds}


def complete_upload(db: Session, dispute_id: str, upload_id: str) -> EvidenceItem:
    try:
        up = db.get(UploadSession, uuid.UUID(upload_id))
    except ValueError:
        up = None
    if up is None or up.dispute_id != dispute_id:
        raise ProofBridgeError(NOT_FOUND, "Upload not found", 404)
    existing = db.query(EvidenceItem).filter(EvidenceItem.upload_id == up.id).first()
    if existing:                      # completing twice is harmless
        return existing
    head = get_storage().head(up.object_key)
    if head is None:
        raise ProofBridgeError(R2_UPLOAD_MISSING, "The file didn't finish uploading. Please retry.", 409)
    if head["size"] > get_settings().max_upload_bytes:
        raise ProofBridgeError(FILE_TOO_LARGE, "File is larger than allowed.", 413)
    up.status, up.size_bytes = "uploaded", head["size"]
    item = EvidenceItem(dispute_id=dispute_id, upload_id=up.id, original_filename=up.original_filename,
                        original_object_key=up.object_key, status="queued")
    db.add(item)
    db.commit()
    metric(db, "upload_completed", dispute_id, item.id, size=head["size"])
    return item
