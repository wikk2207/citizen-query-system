import os
import mimetypes
from datetime import datetime
from functools import wraps

from flask import (
    abort,
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_mail import Message as MailMessage
from flask_login import current_user, login_required
from flask import send_file, flash, redirect, url_for, current_app

from app import db, mail
from app.forms import AchievementForm, ActivityForm, ReportForm
from app.models import (
    Achievement,
    Activity,
    Certificate,
    ClassroomPost,
    ClassroomPostRead,
    Message,
    Notification,
    Report,
    User,
)
from app.services.otp_service import is_mail_configured
from app.services.ocr_service import process_certificate_upload
from app.services.otp_service import send_notification_email, send_plain_email
from app.services.report_service import export_excel, student_portfolio_pdf
from app.utils.achievement_classifier import classify_achievement
from app.utils.helpers import (
    calculate_achievement_points,
    get_badges,
    get_portfolio_level,
    log_action,
    save_upload,
)

bp = Blueprint("student", __name__)

PROBLEM_TYPES = [
    "Complaint Update",
    "Location Information",
    "Additional Evidence",
    "General Question",
    "Other",
]
PRIORITIES = ["Normal", "Urgent", "Low"]


def student_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            flash("Please log in as a student.", "danger")
            return redirect(url_for("auth.login"))
        if not getattr(current_user, "is_student", False):
            from flask_login import logout_user

            logout_user()
            flash("Please log in as a student.", "danger")
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)

    return login_required(decorated)


@bp.route("/reports/upload", methods=["GET", "POST"])
@student_required
def report_upload():
    form = ReportForm()
    if form.validate_on_submit():
        if not current_app.config.get("PERSISTENT_UPLOADS_ENABLED"):
            flash("Report uploads are unavailable until persistent object storage is configured for this deployment.", "danger")
            return redirect(url_for("student.report_upload"))
        file = form.file.data
        filename = f"report_{current_user.id}_{int(datetime.utcnow().timestamp())}_{file.filename}"
        upload_folder = os.path.join(current_app.config["UPLOAD_FOLDER"], "reports")
        os.makedirs(upload_folder, exist_ok=True)
        file_path = os.path.join(upload_folder, filename)
        file.save(file_path)
        rel_path = os.path.relpath(file_path, current_app.static_folder)
        report = Report(
            student_id=current_user.id,
            title=form.title.data,
            description=form.description.data,
            file_path=rel_path,
            status="Submitted",
        )
        db.session.add(report)
        db.session.commit()
        flash("Report uploaded successfully!", "success")
        return redirect(url_for("student.report_upload"))
    return render_template("student/report_upload.html", form=form)


@bp.route("/notifications")
@student_required
def notifications():
    notifications = (
        Notification.query.filter_by(user_id=current_user.id)
        .order_by(Notification.created_at.desc())
        .all()
    )
    return render_template("student/notifications.html", notifications=notifications)


@bp.route("/messages", methods=["GET", "POST"])
@student_required
def messages():
    chat_users = User.query.filter(User.id != current_user.id, User.role.in_(["government", "mentor", "admin"])).order_by(User.full_name).all()
    recent_chat_times = _recent_chat_times(current_user.id)
    chat_users.sort(key=lambda user: _chat_user_sort_key(user, recent_chat_times))
    selected_mentor_id = request.values.get("mentor_id", type=int)
    search = (request.args.get("q") or "").strip()
    if not selected_mentor_id and chat_users:
        selected_mentor_id = chat_users[0].id

    selected_mentor = None
    if selected_mentor_id:
        selected_mentor = User.query.filter(User.id == selected_mentor_id, User.id != current_user.id).first()
        _mark_message_notifications_read()

    if request.method == "POST":
        if not selected_mentor:
            flash("Please select a valid mentor.", "danger")
            return redirect(url_for("student.messages"))

        problem_type = (request.form.get("problem_type") or "Other").strip()
        priority = (request.form.get("priority") or "Normal").strip()
        subject = (request.form.get("subject") or "").strip()
        body = (request.form.get("body") or "").strip()
        if problem_type not in PROBLEM_TYPES:
            problem_type = "Other"
        if priority not in PRIORITIES:
            priority = "Normal"
        attachment_file = request.files.get("attachment")
        attachment_rel, _ = save_upload(attachment_file, "message_attachments")
        if attachment_file and attachment_file.filename and not attachment_rel:
            flash("This file type is not supported for chat sharing.", "danger")
            return redirect(url_for("student.messages", mentor_id=selected_mentor.id))
        if not body and not attachment_rel:
            flash("Please write a message or attach an attachment.", "danger")
            return redirect(url_for("student.messages", mentor_id=selected_mentor.id))
        if not body:
            body = "Shared an attachment."
        display_subject = subject or problem_type
        message_body = _build_chat_body(
            body,
            problem_type=problem_type,
            subject=display_subject,
            priority=priority,
            attachment=attachment_rel,
        )
        msg = Message(
            sender_id=current_user.id,
            receiver_id=selected_mentor.id,
            body=message_body,
            conversation_id=_conversation_id(current_user.id, selected_mentor.id),
        )
        db.session.add(msg)
        db.session.add(
            Notification(
                user_id=selected_mentor.id,
            title="New Citizen Message" if selected_mentor.is_mentor else "New Message",
                message=f"{current_user.full_name} sent a {priority.lower()} {problem_type.lower()} message.",
            )
        )
        db.session.commit()
        _send_chat_email(
            selected_mentor.email,
            f"New message from {current_user.full_name}",
            (
                f"You got a new message from {current_user.full_name}.\n\n"
                f"Problem Type: {problem_type}\nPriority: {priority}\n"
                f"Subject: {display_subject}\n"
                f"Citizen: {current_user.full_name} ({current_user.email})\n"
                f"Phone: {current_user.mobile or 'N/A'}\n"
                f"Address: {current_user.address_line or 'N/A'}\n"
                f"Open: {url_for('mentor.messages', _external=True)}\n\n{body}"
            ),
        )
        flash("Message sent.", "success")
        return redirect(url_for("student.messages", mentor_id=selected_mentor.id))

    messages = []
    status = "Open"
    if selected_mentor:
        conversation = _conversation_id(current_user.id, selected_mentor.id)
        q = Message.query.filter_by(conversation_id=conversation)
        if search:
            q = q.filter(Message.body.ilike(f"%{search}%"))
        messages = q.order_by(Message.created_at.asc()).all()
        all_messages = Message.query.filter_by(conversation_id=conversation).all()
        status = _conversation_status(all_messages)
    return render_template(
        "student/messages.html",
        mentors=chat_users,
        selected_mentor=selected_mentor,
        selected_mentor_id=selected_mentor_id,
        messages=_message_rows(messages),
        shared_materials=_shared_material_rows(messages),
        selected_user_stats=_user_profile_stats(selected_mentor) if selected_mentor else None,
        problem_types=PROBLEM_TYPES,
        priorities=PRIORITIES,
        conversation_status=status,
        search=search,
    )


@bp.route("/messages/<int:mentor_id>/status", methods=["POST"])
@student_required
def message_status(mentor_id):
    mentor = User.query.filter_by(id=mentor_id, role="mentor").first_or_404()
    resolved = request.form.get("status") == "Resolved"
    conversation = _conversation_id(current_user.id, mentor.id)
    Message.query.filter_by(conversation_id=conversation).update({"is_resolved": resolved})
    db.session.commit()
    flash("Conversation marked as resolved." if resolved else "Conversation reopened.", "success")
    return redirect(url_for("student.messages", mentor_id=mentor.id))


@bp.route("/notifications/<int:nid>/read", methods=["POST"])
@student_required
def mark_notification_read(nid):
    n = Notification.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    n.is_read = True
    db.session.commit()
    flash("Notification marked as read.", "success")
    return redirect(url_for("student.notifications"))


@bp.route("/notifications/<int:nid>/unread", methods=["POST"])
@student_required
def mark_notification_unread(nid):
    n = Notification.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    n.is_read = False
    db.session.commit()
    flash("Notification marked as unread.", "info")
    return redirect(url_for("student.notifications"))


@bp.route("/dashboard")
@student_required
def dashboard():
    if current_user.role in ("citizen", "student"):
        return redirect(url_for("civic.citizen_dashboard"))
    achievements = Achievement.query.filter_by(student_id=current_user.id).all()
    activities = Activity.query.filter_by(student_id=current_user.id).all()
    points = calculate_achievement_points(achievements)
    badges = get_badges(achievements, activities)
    portfolio_level = get_portfolio_level(points, len([a for a in achievements if a.status == "Approved"]))
    recent = (
        Achievement.query.filter_by(student_id=current_user.id)
        .order_by(Achievement.updated_at.desc())
        .limit(5)
        .all()
    )
    notifications = (
        Notification.query.filter_by(user_id=current_user.id, is_read=False)
        .order_by(Notification.created_at.desc())
        .limit(5)
        .all()
    )
    # Category analytics for chart
    from collections import Counter
    category_counts = Counter([a.category for a in achievements])
    # Color mapping for categories (extend as needed)
    category_colors = {
        "Hackathon": "#ff9800",
        "Technical": "#2196f3",
        "Soft Skill": "#9c27b0",
        "Sports": "#4caf50",
        "Academic": "#3f51b5",
        "Cultural": "#e91e63",
        "Research": "#607d8b",
        "Certification": "#009688",
        "Other": "#757575",
    }
    stats = {
        "total_achievements": len(achievements),
        "total_activities": len(activities),
        "pending": len([a for a in achievements if a.status in ("Submitted", "Under Review")]),
        "approved": len([a for a in achievements if a.status == "Approved"]),
        "rejected": len([a for a in achievements if a.status == "Rejected"]),
        "points": points,
        "portfolio_level": portfolio_level,
    }
    # Status breakdown
    status_counts = Counter([a.status for a in achievements])
    status_colors = {
        "Draft": "#bdbdbd",
        "Submitted": "#42a5f5",
        "Under Review": "#ffb300",
        "Approved": "#43a047",
        "Rejected": "#e53935",
    }
    status_colors_list = [status_colors.get(s, '#757575') for s in status_counts.keys()]

    # Level distribution
    level_counts = Counter([a.level for a in achievements])
    level_colors = {
        "College": "#1976d2",
        "State": "#0288d1",
        "National": "#c2185b",
        "International": "#7b1fa2",
    }
    level_colors_list = [level_colors.get(l, '#757575') for l in level_counts.keys()]

    # Monthly activity trend (by achievement creation date)
    from collections import defaultdict
    import calendar
    monthly_counts = defaultdict(int)
    for a in achievements:
        if a.created_at:
            key = a.created_at.strftime('%Y-%m')
            monthly_counts[key] += 1
    # Sort by date
    monthly_labels = sorted(monthly_counts.keys())
    monthly_data = [monthly_counts[m] for m in monthly_labels]

    # Prepare color list for chart.js (order matches category_counts.keys())
    category_colors_list = [category_colors.get(cat, '#757575') for cat in category_counts.keys()]
    return render_template(
        "student/dashboard.html",
        stats=stats,
        badges=badges,
        portfolio_level=portfolio_level,
        recent=recent,
        notifications=notifications,
        category_counts=category_counts,
        category_colors_list=category_colors_list,
        status_counts=status_counts,
        status_colors_list=status_colors_list,
        level_counts=level_counts,
        level_colors_list=level_colors_list,
        monthly_labels=monthly_labels,
        monthly_data=monthly_data,
        classroom_posts=_student_classroom_posts().limit(5).all(),
        read_post_ids=_student_read_post_ids(),
    )


@bp.route("/deadlines")
@student_required
def deadlines():
    posts = _student_classroom_posts().all()
    return render_template(
        "student/deadlines.html",
        posts=posts,
        read_post_ids=_student_read_post_ids(),
    )


@bp.route("/deadlines/<int:post_id>/read", methods=["POST"])
@student_required
def deadline_mark_read(post_id):
    post = ClassroomPost.query.filter_by(id=post_id, is_active=True).first_or_404()
    if not _post_matches_student(post, current_user):
        abort(403)
    existing = ClassroomPostRead.query.filter_by(
        post_id=post.id,
        student_id=current_user.id,
    ).first()
    if not existing:
        db.session.add(ClassroomPostRead(post_id=post.id, student_id=current_user.id))
        db.session.commit()
    flash("Marked as read.", "success")
    return redirect(request.referrer or url_for("student.deadlines"))


@bp.route("/achievements")
@student_required
def achievements_list():
    status = request.args.get("status", "")
    category = request.args.get("category", "")
    q = Achievement.query.filter_by(student_id=current_user.id)
    if status:
        q = q.filter_by(status=status)
    if category:
        q = q.filter_by(category=category)
    items = q.order_by(Achievement.created_at.desc()).all()
    # Never redirect for empty results, just show the page with empty list
    return render_template("student/achievements.html", achievements=items, status_filter=status)


@bp.route("/achievements/add", methods=["GET", "POST"])
@student_required
def achievement_add():
    form = AchievementForm()
    _prefill_submission_identity(form)
    if form.validate_on_submit():
        status = "Draft" if form.save_draft.data else "Submitted"
        # AI-based category classification if category is not selected or is 'Other'
        category = form.category.data
        if not category or category == "Other":
            text = f"{form.title.data or ''} {form.description.data or ''}"
            category = classify_achievement(text)
        ach = Achievement(
            student_id=current_user.id,
            branch=form.branch.data,
            year=form.year.data,
            roll_number=form.roll_number.data,
            title=form.title.data,
            category=category,
            event_name=form.event_name.data,
            organizer=form.organizer.data,
            event_date=form.event_date.data,
            rank=form.rank.data,
            level=form.level.data,
            description=form.description.data,
            status=status if status == "Draft" else "Submitted",
        )
        db.session.add(ach)
        db.session.flush()
        if form.certificate.data:
            _attach_certificate(ach, form.certificate.data, achievement_id=ach.id)
        db.session.commit()
        if status == "Submitted":
            _notify_mentors(ach)
        log_action("achievement_create", ach.title)
        flash("Achievement saved." if status == "Draft" else "Achievement submitted for review.", "success")
        return redirect(url_for("student.achievements_list"))
    return render_template("student/achievement_form.html", form=form, action="Add")


@bp.route("/achievements/<int:aid>/edit", methods=["GET", "POST"])
@student_required
def achievement_edit(aid):
    ach = Achievement.query.filter_by(id=aid, student_id=current_user.id).first_or_404()
    form = AchievementForm(obj=ach)
    _prefill_submission_identity(form, ach)
    if form.validate_on_submit():
        ach.branch = form.branch.data
        ach.year = form.year.data
        ach.roll_number = form.roll_number.data
        ach.title = form.title.data
        # AI-based category classification if category is not selected or is 'Other'
        category = form.category.data
        if not category or category == "Other":
            text = f"{form.title.data or ''} {form.description.data or ''}"
            category = classify_achievement(text)
        ach.category = category
        ach.event_name = form.event_name.data
        ach.organizer = form.organizer.data
        ach.event_date = form.event_date.data
        ach.rank = form.rank.data
        ach.level = form.level.data
        ach.description = form.description.data
        if form.save_draft.data:
            ach.status = "Draft"
        elif ach.status in ("Draft", "Rejected"):
            ach.status = "Submitted"
        if form.certificate.data:
            _attach_certificate(ach, form.certificate.data, achievement_id=ach.id)
        db.session.commit()
        flash("Achievement updated.", "success")
        return redirect(url_for("student.achievements_list"))
    return render_template("student/achievement_form.html", form=form, action="Edit", achievement=ach)


@bp.route("/achievements/<int:aid>/delete", methods=["POST"])
@student_required
def achievement_delete(aid):
    ach = Achievement.query.filter_by(id=aid, student_id=current_user.id).first_or_404()
    if ach.certificate:
        db.session.delete(ach.certificate)
    db.session.delete(ach)
    db.session.commit()
    flash("Achievement deleted.", "info")
    return redirect(url_for("student.achievements_list"))


@bp.route("/achievements/<int:aid>/resubmit", methods=["POST"])
@student_required
def achievement_resubmit(aid):
    ach = Achievement.query.filter_by(id=aid, student_id=current_user.id).first_or_404()
    if ach.status == "Rejected":
        ach.status = "Submitted"
        ach.mentor_comment = None
        db.session.commit()
        _notify_mentors(ach)
        flash("Resubmitted for review.", "success")
    return redirect(url_for("student.achievements_list"))


@bp.route("/activities")
@student_required
def activities_list():
    activity_type = request.args.get("activity_type", "")
    q = Activity.query.filter_by(student_id=current_user.id)
    if activity_type:
        q = q.filter_by(activity_type=activity_type)
    items = q.order_by(Activity.created_at.desc()).all()
    return render_template("student/activities.html", activities=items)


@bp.route("/activities/add", methods=["GET", "POST"])
@student_required
def activity_add():
    form = ActivityForm()
    _prefill_submission_identity(form)
    if form.validate_on_submit():
        status = "Draft" if form.save_draft.data else "Submitted"
        # AI-based classification for activity_type if 'Other' or not selected
        activity_type = form.activity_type.data
        if not activity_type or activity_type == "Other":
            text = f"{form.activity_name.data or ''} {form.description.data or ''}"
            activity_type = classify_achievement(text)
        act = Activity(
            student_id=current_user.id,
            branch=form.branch.data,
            year=form.year.data,
            roll_number=form.roll_number.data,
            activity_name=form.activity_name.data,
            activity_type=activity_type,
            role=form.role.data,
            date=form.date.data,
            duration=form.duration.data,
            organizer=form.organizer.data,
            description=form.description.data,
            status=status,
        )
        db.session.add(act)
        db.session.flush()
        if form.document.data:
            _attach_certificate(act, form.document.data, activity_id=act.id)
        db.session.commit()
        flash("Activity saved.", "success")
        return redirect(url_for("student.activities_list"))
    return render_template("student/activity_form.html", form=form, action="Add")


@bp.route("/activities/<int:aid>/edit", methods=["GET", "POST"])
@student_required
def activity_edit(aid):
    act = Activity.query.filter_by(id=aid, student_id=current_user.id).first_or_404()
    form = ActivityForm(obj=act)
    _prefill_submission_identity(form, act)
    if form.validate_on_submit():
        act.branch = form.branch.data
        act.year = form.year.data
        act.roll_number = form.roll_number.data
        act.activity_name = form.activity_name.data
        # AI-based classification for activity_type if 'Other' or not selected
        activity_type = form.activity_type.data
        if not activity_type or activity_type == "Other":
            text = f"{form.activity_name.data or ''} {form.description.data or ''}"
            activity_type = classify_achievement(text)
        act.activity_type = activity_type
        act.role = form.role.data
        act.date = form.date.data
        act.duration = form.duration.data
        act.organizer = form.organizer.data
        act.description = form.description.data
        if form.document.data:
            _attach_certificate(act, form.document.data, activity_id=act.id)
        db.session.commit()
        flash("Activity updated.", "success")
        return redirect(url_for("student.activities_list"))
    return render_template("student/activity_form.html", form=form, action="Edit", activity=act)


@bp.route("/activities/<int:aid>/delete", methods=["POST"])
@student_required
def activity_delete(aid):
    act = Activity.query.filter_by(id=aid, student_id=current_user.id).first_or_404()
    if act.certificate:
        db.session.delete(act.certificate)
    db.session.delete(act)
    db.session.commit()
    flash("Activity deleted.", "info")
    return redirect(url_for("student.activities_list"))


@bp.route("/reports")
@student_required
def reports():
    if current_user.role == "citizen":
        return redirect(url_for("civic.citizen_reports"))
    return render_template("student/reports.html")


@bp.route("/portfolio/pdf")
@student_required
def portfolio_pdf():
    achievements = Achievement.query.filter_by(student_id=current_user.id).all()
    activities = Activity.query.filter_by(student_id=current_user.id).all()
    points = calculate_achievement_points(achievements)
    badges = get_badges(achievements, activities)
    portfolio_level = get_portfolio_level(points, len([a for a in achievements if a.status == "Approved"]))
    pdf = student_portfolio_pdf(current_user, achievements, activities, points, badges, portfolio_level)
    return send_file(
        pdf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"portfolio_{current_user.id}.pdf",
    )


@bp.route("/export/excel")
@student_required
def export_excel_route():
    achievements = Achievement.query.filter_by(student_id=current_user.id).all()
    output = export_excel(achievements, sheet_name="My Achievements")
    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="my_achievements.xlsx",
    )


@bp.route("/portfolio/public")
@student_required
def public_portfolio():
    achievements = Achievement.query.filter_by(
        student_id=current_user.id, status="Approved"
    ).all()
    activities = Activity.query.filter_by(student_id=current_user.id, status="Approved").all()
    points = calculate_achievement_points(achievements)
    return render_template(
        "student/public_portfolio.html",
        student=current_user,
        achievements=achievements,
        activities=activities,
        points=points,
    )

@bp.route("/certificate/<int:cert_id>")
@login_required
def view_certificate(cert_id):
    cert = Certificate.query.get_or_404(cert_id)
    if not _can_access_certificate(cert):
        abort(403)

    certificate_path = _certificate_abs_path(cert)

    if not os.path.exists(certificate_path):
        flash("Certificate file not found. Please upload it again.", "danger")
        return redirect(_certificate_fallback_url())

    return send_file(
        certificate_path,
        mimetype=mimetypes.guess_type(cert.file_name or cert.file_path)[0] or "application/octet-stream",
        as_attachment=False,
        download_name=cert.file_name,
    )


@bp.route("/certificate/<int:cert_id>/download")
@login_required
def download_certificate(cert_id):
    cert = Certificate.query.get_or_404(cert_id)
    if not _can_access_certificate(cert):
        abort(403)
    certificate_path = _certificate_abs_path(cert)
    if not os.path.exists(certificate_path):
        flash("Certificate file not found. Please upload it again.", "danger")
        return redirect(_certificate_fallback_url())
    return send_file(certificate_path, as_attachment=True, download_name=cert.file_name)


@bp.route("/certificate/<int:cert_id>/delete", methods=["POST"])
@student_required
def delete_certificate(cert_id):
    cert = Certificate.query.get_or_404(cert_id)
    owner = _certificate_owner(cert)
    if not owner or owner.id != current_user.id:
        abort(403)
    certificate_path = _certificate_abs_path(cert)
    if os.path.exists(certificate_path):
        os.remove(certificate_path)
    db.session.delete(cert)
    db.session.commit()
    flash("Uploaded document deleted.", "success")
    return redirect(request.referrer or url_for("student.achievements_list"))


def _certificate_owner(cert):
    if cert.achievement:
        return cert.achievement.student
    if cert.activity:
        return cert.activity.student
    return None


def _prefill_submission_identity(form, submission=None):
    if request.method != "GET":
        return
    if not getattr(form.branch, "data", None):
        form.branch.data = (getattr(submission, "branch", None) or current_user.department or "")
    if not getattr(form.year, "data", None):
        form.year.data = (getattr(submission, "year", None) or current_user.year or "")
    if not getattr(form.roll_number, "data", None):
        form.roll_number.data = (
            getattr(submission, "roll_number", None) or current_user.roll_number or ""
        )


def _can_access_certificate(cert):
    owner = _certificate_owner(cert)
    if not owner:
        return False
    return current_user.is_authenticated and (current_user.is_mentor or owner.id == current_user.id)


def _certificate_abs_path(cert):
    static_root = os.path.abspath(current_app.static_folder)
    path = os.path.abspath(os.path.join(static_root, cert.file_path or ""))
    if not path.startswith(static_root):
        abort(404)
    return path


def _certificate_fallback_url():
    if current_user.is_authenticated and current_user.is_mentor:
        return request.referrer or url_for("mentor.submissions")
    return request.referrer or url_for("student.achievements_list")


def _student_classroom_posts():
    department = (current_user.department or "").strip()
    year = (current_user.year or "").strip()
    return ClassroomPost.query.filter(
        ClassroomPost.is_active.is_(True),
        db.or_(
            ClassroomPost.branch.is_(None),
            ClassroomPost.branch == "",
            db.func.lower(ClassroomPost.branch) == department.lower(),
        ),
        db.or_(
            ClassroomPost.year.is_(None),
            ClassroomPost.year == "",
            ClassroomPost.year == year,
        ),
    ).order_by(ClassroomPost.due_at.asc())


def _student_read_post_ids():
    return {
        row[0]
        for row in db.session.query(ClassroomPostRead.post_id)
        .filter_by(student_id=current_user.id)
        .all()
    }


def _post_matches_student(post, student):
    branch_ok = not post.branch or (post.branch or "").strip().lower() == (student.department or "").strip().lower()
    year_ok = not post.year or (post.year or "").strip() == (student.year or "").strip()
    return branch_ok and year_ok


def _attach_certificate(parent, file, achievement_id=None, activity_id=None):
    # Nothing uploaded
    if not file:
        return

    # Existing Certificate object passed during edit: keep current certificate and do nothing
    if hasattr(file, "file_name") and not hasattr(file, "filename"):
        return

    # New uploaded file (Werkzeug FileStorage)
    if hasattr(file, "filename"):
        if not file.filename:
            return
        rel, fname = save_upload(file, "certificates")
        if not rel or not fname:
            return
        ach = parent if achievement_id and hasattr(parent, "title") else None
        existing = None
        if achievement_id:
            existing = Certificate.query.filter_by(achievement_id=achievement_id).first()
        elif activity_id:
            existing = Certificate.query.filter_by(activity_id=activity_id).first()
        ocr = process_certificate_upload(
            rel,
            current_user.full_name,
            achievement=ach,
            certificate_id=existing.id if existing else None,
        )
        if existing:
            cert = existing
        else:
            cert = Certificate()
            db.session.add(cert)
        cert.achievement_id = achievement_id
        cert.activity_id = activity_id
        cert.file_name = fname
        cert.file_path = rel
        cert.file_type = fname.rsplit(".", 1)[-1].lower() if "." in fname else None
        cert.extracted_text = ocr.get("extracted_text", "")
        cert.detected_name = ocr.get("detected_name", "")
        cert.detected_event = ocr.get("detected_event", "")
        cert.detected_date = ocr.get("detected_date", "")
        cert.match_score = ocr.get("match_score", 0)
        cert.verification_status = ocr.get("verification_status", "Pending")
        cert.confidence_score = ocr.get("confidence_score", 0)
        cert.authenticity_score = ocr.get("authenticity_score", 0)
        cert.file_hash = ocr.get("file_hash", "")
        cert.fraud_risk = ocr.get("fraud_risk", "Low")
        cert.fraud_notes = ocr.get("fraud_notes_json", "")
        if achievement_id and parent and hasattr(parent, "event_name") and not parent.event_name:
            if ocr.get("detected_event"):
                parent.event_name = ocr["detected_event"][:200]
        return cert


def _notify_mentors(achievement):
    mentors = User.query.filter_by(role="mentor").all()
    for m in mentors:
        n = Notification(
            user_id=m.id,
            title="New Submission",
            message=f"{current_user.full_name} submitted: {achievement.title}",
        )
        db.session.add(n)
    db.session.commit()


def _conversation_id(first_user_id, second_user_id):
    left, right = sorted([int(first_user_id), int(second_user_id)])
    return f"{left}:{right}"


def _build_chat_body(body, problem_type="Other", subject="Message", priority="Normal", attachment=None):
    lines = [
        f"Problem Type: {problem_type}",
        f"Priority: {priority}",
        f"Subject: {subject}",
    ]
    if attachment:
        lines.append(f"Attachment: {attachment}")
    return "\n".join(lines) + f"\n\n{body}"


def _recent_chat_times(user_id):
    recent = {}
    messages = (
        Message.query.filter((Message.sender_id == user_id) | (Message.receiver_id == user_id))
        .order_by(Message.created_at.desc())
        .all()
    )
    for message in messages:
        partner_id = message.receiver_id if message.sender_id == user_id else message.sender_id
        recent.setdefault(partner_id, message.created_at)
    return recent


def _chat_user_sort_key(user, recent_chat_times):
    last_message_at = recent_chat_times.get(user.id)
    last_message_rank = -(last_message_at.timestamp()) if last_message_at else 0
    return (
        0 if last_message_at else 1,
        last_message_rank,
        0 if user.is_mentor else 1,
        (user.full_name or "").lower(),
    )


def _message_rows(messages):
    return [{"message": m, "meta": _message_meta(m)} for m in messages]


def _shared_material_rows(messages):
    rows = []
    for message in messages:
        meta = _message_meta(message)
        if meta.get("attachment"):
            rows.append({"message": message, "meta": meta})
    return rows


def _user_profile_stats(user):
    if not user:
        return None
    achievements = Achievement.query.filter_by(student_id=user.id).all() if user.is_student else []
    activities = Activity.query.filter_by(student_id=user.id).all() if user.is_student else []
    approved = [a for a in achievements if a.status == "Approved"]
    skills_text = user.mentor_skills or ""
    skills = [s.strip() for s in skills_text.replace("\n", ",").split(",") if s.strip()]
    return {
        "skills": skills[:12],
        "bio": user.mentor_bio or "",
        "approved_count": len(approved),
        "achievement_count": len(achievements),
        "activity_count": len(activities),
        "points": calculate_achievement_points(achievements),
    }


def _message_meta(message):
    meta = {"body": message.body or "", "attachment": None}
    header, sep, rest = (message.body or "").partition("\n\n")
    if sep:
        parsed = {}
        for line in header.splitlines():
            key, separator, value = line.partition(":")
            if separator:
                parsed[key.strip().lower().replace(" ", "_")] = value.strip()
        if parsed:
            meta.update(parsed)
            meta["body"] = rest
    attachment = meta.get("attachment")
    if attachment:
        meta["attachment_name"] = attachment.rsplit("/", 1)[-1]
        meta["attachment_type"] = _attachment_type(attachment)
    return meta


def _attachment_type(path):
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in {"png", "jpg", "jpeg", "gif", "webp"}:
        return "image"
    if ext in {"mp4", "webm", "mov", "mkv", "avi"}:
        return "video"
    if ext in {"mp3", "wav", "m4a"}:
        return "audio"
    if ext == "pdf":
        return "pdf"
    return "file"


def _conversation_status(messages):
    if messages and all(m.is_resolved for m in messages):
        return "Resolved"
    if any(m.sender_id != current_user.id for m in messages):
        return "In Progress"
    return "Open"


def _mark_message_notifications_read():
    q = Notification.query.filter(
        Notification.user_id == current_user.id,
        Notification.is_read.is_(False),
        Notification.title.in_(["Mentor Reply", "New Message"]),
    )
    if not q.first():
        return
    try:
        q.update({"is_read": True}, synchronize_session=False)
        db.session().expire_on_commit = False
        db.session.commit()
    except Exception:
        db.session.rollback()


def _send_chat_email(recipient, subject, body):
    return send_plain_email(recipient, subject, body)
