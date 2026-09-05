"""Certificate fraud detection — OCR cross-check, duplicate hash, image quality."""
import hashlib
import json
import os

from flask import current_app
from rapidfuzz import fuzz

try:
    import cv2
    import numpy as np
    HAS_CV = True
except (ImportError, OSError):
    HAS_CV = False


def _full_path(file_path):
    if os.path.isabs(file_path):
        return file_path
    base = os.path.dirname(current_app.root_path)
    return os.path.join(base, "static", file_path)


def compute_file_hash(file_path):
    path = _full_path(file_path)
    if not os.path.exists(path):
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_duplicate_hash(file_hash, exclude_id=None):
    if not file_hash:
        return None
    from app.models import Certificate
    q = Certificate.query.filter_by(file_hash=file_hash)
    if exclude_id:
        q = q.filter(Certificate.id != exclude_id)
    return q.first()


def _image_quality_score(file_path):
    if not HAS_CV:
        return 0.5, []
    path = _full_path(file_path)
    if path.rsplit(".", 1)[-1].lower() == "pdf":
        return 0.6, []
    img = cv2.imread(path)
    if img is None:
        return 0.3, ["Could not read image"]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    notes = []
    score = 0.55
    if cv2.Laplacian(gray, cv2.CV_64F).var() < 50:
        score -= 0.2
        notes.append("Image appears blurry")
    return max(0.0, min(1.0, score)), notes


def _cross_validate_fields(ocr_result, student_name, achievement=None):
    notes = []
    penalty = 0.0
    match = ocr_result.get("match_score", 0)
    if match < 0.45:
        penalty += 0.35
        notes.append("Name does not match profile")
    text = (ocr_result.get("extracted_text") or "").lower()
    if len(text) < 40:
        penalty += 0.2
        notes.append("Too little text extracted")
    return penalty, notes


def scan_certificate(file_path, student_name, ocr_result, achievement=None, certificate_id=None):
    file_hash = compute_file_hash(file_path)
    dup = _find_duplicate_hash(file_hash, exclude_id=certificate_id)
    img_score, img_notes = _image_quality_score(file_path)
    field_penalty, field_notes = _cross_validate_fields(ocr_result, student_name, achievement)

    fraud_score = field_penalty + (0.45 if dup else 0) + max(0, 0.55 - img_score) * 0.4
    fraud_notes = list(img_notes) + list(field_notes)
    if dup:
        fraud_notes.append("Duplicate certificate file detected")

    match = ocr_result.get("match_score", 0)
    conf = ocr_result.get("confidence_score", 0.5)
    combined = max(0.0, min(1.0, conf - fraud_score * 0.5))

    if fraud_score >= 0.55:
        status, risk = "Suspected Fake", "High"
    elif dup or fraud_score >= 0.35:
        status, risk = "Manual Review Required", "Medium"
    elif match >= 0.85:
        status, risk = "Verified", "Low"
    elif match >= 0.65:
        status, risk = "Likely Authentic", "Low"
    elif match > 0 and match < 0.5:
        status, risk = "Name Mismatch", "High"
    else:
        status, risk = ocr_result.get("verification_status", "Manual Review Required"), "Medium"

    return {
        **ocr_result,
        "file_hash": file_hash,
        "verification_status": status,
        "authenticity_score": round(combined, 3),
        "confidence_score": round(combined, 3),
        "confidence_percent": int(round(combined * 100)),
        "fraud_risk": risk,
        "fraud_score": round(min(1.0, fraud_score), 3),
        "fraud_notes": fraud_notes,
        "fraud_notes_json": json.dumps(fraud_notes),
        "is_duplicate": dup is not None,
        "scanner_verdict": (
            "Certificate appears authentic."
            if status in ("Verified", "Likely Authentic")
            else "Certificate may be fake — mentor review required."
        ),
    }
