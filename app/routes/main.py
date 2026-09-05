from flask import Blueprint, current_app, jsonify, render_template, redirect, url_for, session, request
from flask_login import current_user
from sqlalchemy import text

from app import db
from app.models import Achievement, Certificate, ClassroomPost, User

bp = Blueprint("main", __name__)

@bp.route("/language/<language>")
def set_language(language):
    if language not in ("en", "hi", "mr"): language = "en"
    session["language"] = language
    if current_user.is_authenticated:
        current_user.preferred_language = language
        from app import db
        db.session.commit()
    return redirect(request.referrer or url_for("main.index"))


@bp.route("/")
@bp.route("/home")
def index():
    return render_template("index.html")


@bp.route("/login")
@bp.route("/student-login")
def student_login_redirect():
    return redirect(url_for("auth.login"))


@bp.route("/mentor-login")
def mentor_login_redirect():
    return redirect(url_for("auth.mentor_login"))


@bp.route("/dashboard")
def dashboard_redirect():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    if current_user.role in ("government", "mentor", "admin"):
        from app.services.mentor_auth import mentor_session_verified
        if not mentor_session_verified():
            return redirect(url_for("auth.mentor_verify_otp"))
        return redirect(url_for("civic.government_dashboard"))
    return redirect(url_for("civic.citizen_dashboard"))


@bp.route("/access-denied")
def access_denied():
    return render_template("access_denied.html"), 403


@bp.route("/healthz")
def healthz():
    try:
        db.session.execute(text("select 1"))
        return jsonify({"status": "ok", "database": "ok"})
    except Exception:
        current_app.logger.exception("Database health check failed")
        return jsonify({"status": "error", "database": "unavailable"}), 503


@bp.route("/healthz/deep")
def healthz_deep():
    result = {"status": "ok", "checks": {}}
    try:
        result["checks"]["database"] = "ok"
        result["checks"]["users"] = User.query.count()
        result["checks"]["mentors"] = User.query.filter_by(role="mentor").count()
        result["checks"]["students"] = User.query.filter_by(role="student").count()
        result["checks"]["achievements"] = Achievement.query.count()
        result["checks"]["certificates"] = Certificate.query.count()
        result["checks"]["classroom_posts"] = ClassroomPost.query.count()
        result["checks"]["mail_server"] = "configured" if _mail_ready() else "missing"
        return jsonify(result)
    except Exception:
        current_app.logger.exception("Deep health check failed")
        result["status"] = "error"
        result["error"] = "A dependency is unavailable. Check server logs."
        return jsonify(result), 503


def _mail_ready():
    from flask import current_app

    return bool(
        current_app.config.get("MAIL_SERVER")
        and current_app.config.get("MAIL_USERNAME")
        and current_app.config.get("MAIL_PASSWORD")
    )


@bp.route("/healthz/last-error")
def healthz_last_error():
    return jsonify(current_app.config.get("LAST_ERROR") or {"status": "no error captured yet"})
