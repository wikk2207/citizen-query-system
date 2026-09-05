import os
import re
import shutil
from datetime import datetime

from flask import current_app
from rapidfuzz import fuzz

try:
    import cv2
    import pytesseract
    from PIL import Image

    HAS_OCR = True
except (ImportError, OSError):
    HAS_OCR = False


def _configure_tesseract():
    cmd = current_app.config.get("TESSERACT_CMD")
    if cmd and HAS_OCR:
        pytesseract.pytesseract.tesseract_cmd = cmd


def _ocr_runtime_error():
    """Return a user-facing prerequisite error without probing OCR during app startup."""
    if not HAS_OCR:
        return "OCR libraries are not installed"
    configured = current_app.config.get("TESSERACT_CMD")
    executable = configured or shutil.which("tesseract")
    if not executable or (configured and not os.path.isfile(configured)):
        return "OCR is unavailable because the Tesseract runtime is not installed on this server."
    try:
        pytesseract.get_tesseract_version()
    except pytesseract.TesseractNotFoundError:
        return "OCR is unavailable because the Tesseract runtime is not installed on this server."
    return None


def extract_text_from_file(file_path):
    if not HAS_OCR:
        return "", 0.0, "OCR libraries not installed"

    _configure_tesseract()
    runtime_error = _ocr_runtime_error()
    if runtime_error:
        return "", 0.0, runtime_error
    full_path = file_path
    if not os.path.isabs(file_path):
        base = os.path.dirname(current_app.root_path)
        full_path = os.path.join(base, "static", file_path)

    if not os.path.exists(full_path):
        return "", 0.0, "File not found"

    ext = full_path.rsplit(".", 1)[-1].lower()
    try:
        if ext == "pdf":
            try:
                from pdf2image import convert_from_path

                pages = convert_from_path(full_path, first_page=1, last_page=1)
                if not pages:
                    return "", 0.0, "Empty PDF"
                img = pages[0]
            except ImportError:
                return "", 0.0, "PDF OCR requires pdf2image"
            except Exception as exc:
                # Poppler is not available in Vercel's Python runtime by default.
                return "", 0.0, f"PDF OCR is unavailable on this server: {exc}"
        else:
            img = Image.open(full_path)

        if ext != "pdf":
            img_array = cv2.imread(full_path)
            if img_array is not None:
                gray = cv2.cvtColor(img_array, cv2.COLOR_BGR2GRAY)
                gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
                text = pytesseract.image_to_string(gray)
            else:
                text = pytesseract.image_to_string(img)
        else:
            text = pytesseract.image_to_string(img)

        conf_data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        confs = [int(c) for c in conf_data["conf"] if str(c).isdigit() and int(c) > 0]
        avg_conf = sum(confs) / len(confs) if confs else 50.0
        return text.strip(), avg_conf / 100.0, None
    except Exception as e:
        current_app.logger.warning("OCR failed: %s", e)
        return "", 0.0, str(e)


def _extract_name_candidates(text):
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    candidates = []
    for line in lines[:15]:
        if re.match(r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+$", line):
            candidates.append(line)
        if "certificate" in line.lower() and len(line) < 80:
            continue
    return candidates


def _extract_event(text):
    patterns = [
        r"(?:event|competition|hackathon|workshop|seminar)[:\s]+(.+)",
        r"(?:for|in)\s+(.{10,80})",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1).strip()[:200]
    lines = text.split("\n")
    for line in lines:
        if any(k in line.lower() for k in ("hackathon", "workshop", "olympiad", "championship")):
            return line.strip()[:200]
    return ""


def _extract_date(text):
    patterns = [
        r"\b(\d{1,2}[\-/]\d{1,2}[\-/]\d{2,4})\b",
        r"\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1)
    return ""


def verify_certificate(text, student_name, confidence_base=0.5):
    if not text:
        return {
            "detected_name": "",
            "detected_event": "",
            "detected_date": "",
            "match_score": 0.0,
            "verification_status": "Manual Review Required",
            "confidence_score": confidence_base * 0.3,
            "authenticity_score": 0.3,
        }

    candidates = _extract_name_candidates(text)
    detected_name = candidates[0] if candidates else ""
    best_score = 0.0
    for cand in candidates:
        score = fuzz.token_sort_ratio(student_name.lower(), cand.lower())
        if score > best_score:
            best_score = score
            detected_name = cand

    if not detected_name and student_name.lower() in text.lower():
        detected_name = student_name
        best_score = fuzz.partial_ratio(student_name.lower(), text.lower())

    detected_event = _extract_event(text)
    detected_date = _extract_date(text)

    match_score = best_score / 100.0
    conf = min(1.0, confidence_base * 0.6 + match_score * 0.4)
    auth = min(1.0, conf * 0.7 + (0.3 if len(text) > 100 else 0.1))

    if match_score >= 0.85 and conf >= 0.6:
        status = "Verified"
    elif match_score >= 0.5:
        status = "Manual Review Required"
    elif match_score > 0:
        status = "Name Mismatch"
    elif conf < 0.4:
        status = "Low Confidence"
    else:
        status = "Manual Review Required"

    return {
        "detected_name": detected_name,
        "detected_event": detected_event,
        "detected_date": detected_date,
        "match_score": round(match_score, 3),
        "verification_status": status,
        "confidence_score": round(conf, 3),
        "authenticity_score": round(auth, 3),
    }


def process_certificate_upload(file_path, student_name, achievement=None, certificate_id=None):
    from app.services.certificate_scanner import scan_certificate

    text, ocr_conf, err = extract_text_from_file(file_path)
    if err and not text:
        text = f"[OCR unavailable: {err}]"
    result = verify_certificate(text, student_name, confidence_base=ocr_conf or 0.5)
    result["extracted_text"] = text[:5000]
    return scan_certificate(
        file_path, student_name, result, achievement=achievement, certificate_id=certificate_id
    )
