from collections import Counter
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from app import db
from app.models import Achievement, Activity, Certificate, Notification, User
from app.services.ocr_service import process_certificate_upload
from app.utils.helpers import calculate_achievement_points, save_upload

bp = Blueprint("api", __name__)


def _student_only():
    if not current_user.is_authenticated or not current_user.is_student:
        return False
    return True


@bp.route("/notifications")
@login_required
def notifications():
    items = (
        Notification.query.filter_by(user_id=current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(20)
        .all()
    )
    return jsonify([
        {
            "id": n.id,
            "title": n.title,
            "message": n.message,
            "is_read": n.is_read,
            "created_at": n.created_at.isoformat() if n.created_at else None,
            "url": _notification_url(n),
        }
        for n in items
    ])


@bp.route("/notifications/<int:nid>/read", methods=["POST"])
@login_required
def mark_read(nid):
    n = Notification.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    n.is_read = True
    db.session.commit()
    return jsonify({"ok": True})


def _notification_url(notification):
    if notification.title in ("New Student Message", "Mentor Reply"):
        if current_user.is_student:
            return url_for("student.messages")
        if current_user.is_mentor:
            return url_for("mentor.messages")
    if notification.title == "Review Update" and current_user.is_student:
        return url_for("student.notifications")
    if notification.title == "New Submission" and current_user.is_mentor:
        return url_for("mentor.submissions")
    return url_for("student.notifications") if current_user.is_student else "#"


@bp.route("/analytics/student")
@login_required
def student_analytics():
    if not _student_only():
        return jsonify({"error": "Forbidden"}), 403
    achievements = Achievement.query.filter_by(student_id=current_user.id).all()
    activities = Activity.query.filter_by(student_id=current_user.id).all()

    by_category = Counter(a.category for a in achievements if a.status == "Approved")
    by_level = Counter(a.level for a in achievements if a.status == "Approved")
    by_status = Counter(a.status for a in achievements)

    monthly = Counter()
    for act in activities:
        if act.date:
            monthly[act.date.strftime("%Y-%m")] += 1

    return jsonify({
        "category": dict(by_category),
        "level": dict(by_level),
        "status": dict(by_status),
        "monthly": dict(sorted(monthly.items())),
        "points": calculate_achievement_points(achievements),
        "total_achievements": len(achievements),
        "total_activities": len(activities),
        "approved": len([a for a in achievements if a.status == "Approved"]),
        "pending": len([a for a in achievements if a.status in ("Submitted", "Under Review")]),
        "rejected": len([a for a in achievements if a.status == "Rejected"]),
    })


@bp.route("/analytics/mentor")
@login_required
def mentor_analytics():
    if not current_user.is_mentor:
        return jsonify({"error": "Forbidden"}), 403

    achievements = Achievement.query.filter(Achievement.status != "Draft").all()
    by_dept = Counter()
    by_cat = Counter()
    status_counts = Counter(a.status for a in achievements)

    for a in achievements:
        if a.student:
            by_dept[a.student.department or "Unknown"] += 1
        by_cat[a.category] += 1

    students = User.query.filter_by(role="student").all()
    top = []
    for s in students:
        ach = Achievement.query.filter_by(student_id=s.id, status="Approved").all()
        pts = calculate_achievement_points(ach)
        if ach:
            top.append({"name": s.full_name, "points": pts, "approved": len(ach), "department": s.department})
    top.sort(key=lambda x: x["points"], reverse=True)

    monthly = Counter()
    for a in achievements:
        if a.created_at:
            monthly[a.created_at.strftime("%Y-%m")] += 1

    mismatches = Certificate.query.filter(
        Certificate.verification_status == "Name Mismatch"
    ).count()

    return jsonify({
        "department": dict(by_dept),
        "category": dict(by_cat),
        "status": dict(status_counts),
        "monthly": dict(sorted(monthly.items())),
        "top_students": top[:10],
        "name_mismatches": mismatches,
        "pending": status_counts.get("Submitted", 0) + status_counts.get("Under Review", 0),
    })


@bp.route("/search/achievements")
@login_required
def search_achievements():
    q = request.args.get("q", "").strip().lower()
    category = request.args.get("category", "")
    status = request.args.get("status", "")

    query = Achievement.query
    if current_user.is_student:
        query = query.filter_by(student_id=current_user.id)
    if category:
        query = query.filter_by(category=category)
    if status:
        query = query.filter_by(status=status)
    if q:
        query = query.filter(
            db.or_(
                Achievement.title.ilike(f"%{q}%"),
                Achievement.event_name.ilike(f"%{q}%"),
            )
        )
    results = query.order_by(Achievement.created_at.desc()).limit(50).all()
    return jsonify([
        {
            "id": a.id,
            "title": a.title,
            "category": a.category,
            "status": a.status,
            "event_name": a.event_name,
            "student": a.student.full_name if a.student else "",
        }
        for a in results
    ])


@bp.route("/leaderboard")
def leaderboard():
    students = User.query.filter_by(role="student").all()
    board = []
    for s in students:
        ach = Achievement.query.filter_by(student_id=s.id, status="Approved").all()
        pts = calculate_achievement_points(ach)
        board.append({
            "name": s.full_name,
            "department": s.department,
            "year": s.year,
            "points": pts,
            "count": len(ach),
        })
    board.sort(key=lambda x: x["points"], reverse=True)
    return jsonify(board[:20])


@bp.route("/ocr/preview", methods=["POST"])
@login_required
def ocr_preview():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file provided"}), 400
    try:
        rel, _ = save_upload(file, "temp_ocr")
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    result = process_certificate_upload(rel, current_user.full_name)
    return jsonify(result)


@bp.route("/toggle-dark-mode", methods=["POST"])
@login_required
def toggle_dark():
    current_user.dark_mode = not current_user.dark_mode
    db.session.commit()
    return jsonify({"dark_mode": current_user.dark_mode})


@bp.route("/commands")
@login_required
def commands():
    from app.services.command_service import get_command_registry, search_commands

    q = request.args.get("q", "")
    if q:
        return jsonify({"results": search_commands(q)})
    return jsonify({"results": get_command_registry()})


@bp.route("/commands/search")
def commands_public():
    from app.services.command_service import search_commands

    return jsonify({"results": search_commands(request.args.get("q", ""))})


@bp.route("/suggestions/achievements")
@login_required
def achievement_suggestions():
    if not _student_only():
        return jsonify({"error": "Forbidden"}), 403
    from app.services.suggestion_service import get_achievement_suggestions

    return jsonify({"suggestions": get_achievement_suggestions(current_user.id)})
