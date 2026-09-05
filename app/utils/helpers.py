import os
import uuid
from datetime import datetime

from flask import current_app
from flask_login import current_user
from werkzeug.utils import secure_filename

from app import db
from app.models import AuditLog


def allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower()
        in current_app.config["ALLOWED_EXTENSIONS"]
    )


def save_upload(file, subfolder="certificates"):
    # Accept only uploaded files
    if not file or not hasattr(file, "filename") or not file.filename:
        return None, None
    if not allowed_file(file.filename):
        return None, None
    if not current_app.config.get("PERSISTENT_UPLOADS_ENABLED"):
        raise RuntimeError(
            "File uploads are unavailable until persistent object storage is configured for this deployment."
        )
    ext = file.filename.rsplit(".", 1)[1].lower()
    safe = secure_filename(file.filename.rsplit(".", 1)[0])
    unique = f"{safe}_{uuid.uuid4().hex[:8]}.{ext}"
    folder = os.path.join(current_app.config["UPLOAD_FOLDER"], subfolder)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, unique)
    file.save(path)
    rel = os.path.join("uploads", subfolder, unique).replace("\\", "/")
    return rel, unique


def log_action(action, details=""):
    if current_user.is_authenticated:
        entry = AuditLog(
            user_id=current_user.id,
            action=action,
            details=details[:500],
        )
        db.session.add(entry)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


def calculate_achievement_points(achievements):
    cfg = current_app.config
    total = 0
    for a in achievements:
        if a.status != "Approved":
            continue
        total += cfg.get("LEVEL_POINTS", {}).get(a.level or "College", 10)
        rank_key = (a.rank or "").split()[0] if a.rank else ""
        for k, v in cfg.get("RANK_BONUS", {}).items():
            if k.lower() in (a.rank or "").lower():
                total += v
                break
    return total


def get_portfolio_level(points, approved_count=0):
    if points >= 500 or approved_count >= 25:
        return {
            "name": "Elite Achiever",
            "label": "Level 5",
            "next": None,
            "color": "#111827",
        }
    if points >= 300 or approved_count >= 15:
        return {
            "name": "Campus Champion",
            "label": "Level 4",
            "next": "500 pts or 25 approved achievements",
            "color": "#7f1d1d",
        }
    if points >= 150 or approved_count >= 8:
        return {
            "name": "Top Performer",
            "label": "Level 3",
            "next": "300 pts or 15 approved achievements",
            "color": "#7c2d12",
        }
    if points >= 60 or approved_count >= 3:
        return {
            "name": "Rising Star",
            "label": "Level 2",
            "next": "150 pts or 8 approved achievements",
            "color": "#065f46",
        }
    return {
        "name": "Portfolio Starter",
        "label": "Level 1",
        "next": "60 pts or 3 approved achievements",
        "color": "#1e3a8a",
    }


def get_badges(achievements, activities):
    badges = []
    approved = [a for a in achievements if a.status == "Approved"]
    cats = {(a.category or "").strip() for a in approved}
    titles = " ".join([a.title or "" for a in approved]).lower()
    activity_count = len(activities)
    approved_count = len(approved)
    points = calculate_achievement_points(achievements)

    def add(name):
        if name not in badges:
            badges.append(name)

    if approved_count >= 1:
        add("First Win")
    if approved_count >= 5:
        add("Consistent Achiever")
    if approved_count >= 10:
        add("Achievement Pro")
    if approved_count >= 25:
        add("Top Performer")
    if points >= 150:
        add("Point Builder")
    if points >= 300:
        add("High Scorer")
    if "Research" in cats:
        add("Research Starter")
    if len([a for a in approved if (a.category or "") == "Research"]) >= 3:
        add("Research Scholar")
    if "Sports" in cats:
        add("Sports Champion")
    if "Technical" in cats or "hackathon" in titles:
        add("Tech Explorer")
    if "hackathon" in titles or any((a.event_name or "").lower().find("hackathon") >= 0 for a in approved):
        add("Hackathon Hero")
    if "Certification" in cats:
        add("Certified Learner")
    if "Leadership" in cats:
        add("Leadership Spark")
    if activity_count >= 5:
        add("Active Participant")
    if activity_count >= 10:
        add("Campus Contributor")
    return badges
