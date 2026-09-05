from datetime import datetime
from functools import wraps

from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_mail import Message as MailMessage
from flask_login import current_user, login_required

from app import db, mail
from app.forms import ClassroomPostForm, MentorReviewForm
from app.models import Achievement, Activity, Certificate, ClassroomPost, Complaint, Message, Notification, User
from app.services.otp_service import is_mail_configured
from app.services.otp_service import send_notification_email, send_plain_email
from app.services.report_service import (
    department_report_pdf,
    civic_department_pdf,
    export_civic_csv,
    export_civic_excel,
    export_comprehensive_excel,
    export_csv,
    export_excel,
)
from app.utils.helpers import calculate_achievement_points, log_action, save_upload

bp = Blueprint("mentor", __name__)

PROBLEM_TYPES = [
    "Study Material",
    "Achievement",
    "Activity",
    "Certificate Upload",
    "Report",
    "Account",
    "Other",
]
PRIORITIES = ["Normal", "Urgent", "Low"]
CONVERSATION_STATUSES = ["Open", "In Progress", "Resolved"]


def mentor_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        from app.services.mentor_auth import is_whitelisted_mentor_email, mentor_session_verified

        if not current_user.is_mentor:
            flash("Access denied. Mentor role required.", "danger")
            return redirect(url_for("main.access_denied"))
        if not is_whitelisted_mentor_email(current_user.email):
            flash("Your account is not on the approved mentor list.", "danger")
            return redirect(url_for("main.access_denied"))
        if not mentor_session_verified():
            flash("Complete mentor OTP verification to continue.", "warning")
            return redirect(url_for("auth.mentor_verify_otp"))
        return f(*args, **kwargs)

    return decorated


@bp.route("/dashboard")
@mentor_required
def dashboard():
    if current_user.role in ("government", "mentor", "admin"):
        return redirect(url_for("civic.government_dashboard"))
    achievements = Achievement.query.filter(Achievement.status != "Draft").all()
    pending = [a for a in achievements if a.status in ("Submitted", "Under Review")]
    approved_today = [
        a
        for a in achievements
        if a.status == "Approved"
        and a.reviewed_at
        and a.reviewed_at.date() == datetime.utcnow().date()
    ]
    rejected = [a for a in achievements if a.status == "Rejected"]
    mismatches = Certificate.query.filter_by(verification_status="Name Mismatch").count()
    suspected_fake = Certificate.query.filter_by(verification_status="Suspected Fake").count()
    certs_uploaded = Certificate.query.count()
    student_ids = set()
    for c in Certificate.query.all():
        if c.achievement and c.achievement.student_id:
            student_ids.add(c.achievement.student_id)
        if c.activity and c.activity.student_id:
            student_ids.add(c.activity.student_id)
    students_with_certs = len(student_ids)
    verified_certs = Certificate.query.filter(
        Certificate.verification_status.in_(["Verified", "Likely Authentic"])
    ).count()
    students = User.query.filter_by(role="student").count()

    top = []
    for s in User.query.filter_by(role="student").limit(50).all():
        ach = Achievement.query.filter_by(student_id=s.id, status="Approved").all()
        pts = calculate_achievement_points(ach)
        if ach:
            top.append({"student": s, "points": pts, "count": len(ach)})
    top.sort(key=lambda x: x["points"], reverse=True)

    stats = {
        "students": students,
        "pending": len(pending),
        "approved_today": len(approved_today),
        "rejected": len(rejected),
        "mismatches": mismatches,
        "suspected_fake": suspected_fake,
        "certs_uploaded": certs_uploaded,
        "students_with_certs": students_with_certs,
        "verified_certs": verified_certs,
        "total_submissions": len(achievements),
        "active_deadlines": ClassroomPost.query.filter_by(post_type="deadline", is_active=True).count(),
        "upcoming_events": ClassroomPost.query.filter_by(post_type="event", is_active=True).count(),
    }
    student_rows = []
    for s in User.query.filter_by(role="student").order_by(User.full_name).all():
        ach = Achievement.query.filter_by(student_id=s.id).all()
        cert_count = sum(1 for a in ach if a.certificate)
        student_rows.append({
            "student": s,
            "achievements": len(ach),
            "certificates": cert_count,
            "approved": len([a for a in ach if a.status == "Approved"]),
        })
    return render_template(
        "mentor/dashboard.html",
        stats=stats,
        top_students=top[:10],
        recent_pending=pending[:8],
        student_rows=student_rows,
        classroom_posts=ClassroomPost.query.filter_by(is_active=True).order_by(ClassroomPost.due_at.asc()).limit(6).all(),
    )


@bp.route("/deadlines", methods=["GET", "POST"])
@mentor_required
def deadlines():
    form = ClassroomPostForm()
    if form.validate_on_submit():
        post = ClassroomPost(
            mentor_id=current_user.id,
            post_type=form.post_type.data,
            title=form.title.data.strip(),
            description=(form.description.data or "").strip(),
            due_at=form.due_at.data,
            branch=(form.branch.data or "").strip() or None,
            year=form.year.data or None,
            action_label=(form.action_label.data or "").strip() or None,
            action_url=(form.action_url.data or "").strip() or None,
            is_active=True,
        )
        db.session.add(post)
        db.session.flush()
        _notify_students_for_classroom_post(post)
        db.session.commit()
        flash("Deadline/event published to students.", "success")
        return redirect(url_for("mentor.deadlines"))

    posts = ClassroomPost.query.order_by(ClassroomPost.created_at.desc()).all()
    return render_template("mentor/deadlines.html", form=form, posts=posts)


@bp.route("/deadlines/<int:post_id>/toggle", methods=["POST"])
@mentor_required
def deadline_toggle(post_id):
    post = ClassroomPost.query.get_or_404(post_id)
    post.is_active = not post.is_active
    db.session.commit()
    flash("Post updated.", "success")
    return redirect(url_for("mentor.deadlines"))


@bp.route("/deadlines/<int:post_id>/delete", methods=["POST"])
@mentor_required
def deadline_delete(post_id):
    post = ClassroomPost.query.get_or_404(post_id)
    db.session.delete(post)
    db.session.commit()
    flash("Deadline/event deleted.", "info")
    return redirect(url_for("mentor.deadlines"))


@bp.route("/submissions")
@mentor_required
def submissions():
    status = request.args.get("status", "")
    category = request.args.get("category", "")
    search = request.args.get("q", "").strip()
    dept = request.args.get("department", "")
    year = request.args.get("year", "")

    q = Achievement.query.join(User, Achievement.student_id == User.id).filter(Achievement.status != "Draft")
    if status:
        q = q.filter(Achievement.status == status)
    if category:
        q = q.filter(Achievement.category == category)
    if search:
        q = q.filter(
            db.or_(
                Achievement.title.ilike(f"%{search}%"),
                User.full_name.ilike(f"%{search}%"),
            )
        )
    if dept:
        q = q.filter(db.or_(Achievement.branch == dept, User.department == dept))
    if year:
        q = q.filter(db.or_(Achievement.year == year, User.year == year))
    items = q.order_by(Achievement.created_at.desc()).all()
    user_departments = db.session.query(User.department).filter(User.role == "student").distinct().all()
    submission_branches = db.session.query(Achievement.branch).filter(Achievement.branch.isnot(None)).distinct().all()
    user_years = db.session.query(User.year).filter(User.role == "student").distinct().all()
    submission_years = db.session.query(Achievement.year).filter(Achievement.year.isnot(None)).distinct().all()
    departments = sorted({d[0] for d in user_departments + submission_branches if d[0]})
    years = sorted({y[0] for y in user_years + submission_years if y[0]})
    return render_template(
        "mentor/submissions.html",
        submissions=items,
        status_filter=status,
        category_filter=category,
        departments=departments,
        department_filter=dept,
        years=years,
        year_filter=year,
    )


@bp.route("/submissions/<int:aid>", methods=["GET", "POST"])
@mentor_required
def review_submission(aid):
    ach = Achievement.query.get_or_404(aid)
    form = MentorReviewForm()
    if form.validate_on_submit():
        ach.mentor_comment = form.mentor_comment.data
        ach.reviewed_by = current_user.id
        ach.reviewed_at = datetime.utcnow()
        if form.submit_approve.data:
            ach.status = "Approved"
            msg = f"Your achievement '{ach.title}' was approved."
            send_notification_email(
                ach.student,
                "Achievement Approved",
                "emails/approved.html",
                achievement=ach,
            )
        elif form.submit_reject.data:
            ach.status = "Rejected"
            msg = f"Your achievement '{ach.title}' was rejected. {ach.mentor_comment or ''}"
            send_notification_email(
                ach.student,
                "Achievement Rejected",
                "emails/rejected.html",
                achievement=ach,
            )
        else:
            return render_template("mentor/review.html", achievement=ach, form=form)

        n = Notification(user_id=ach.student_id, title="Review Update", message=msg)
        db.session.add(n)
        db.session.commit()
        log_action("review", f"{ach.status}: {ach.title}")
        flash(f"Submission {ach.status.lower()}.", "success")
        return redirect(url_for("mentor.submissions"))

    if ach.status == "Submitted":
        ach.status = "Under Review"
        db.session.commit()
    return render_template("mentor/review.html", achievement=ach, form=form)


@bp.route("/submissions/<int:aid>/bulk-approve", methods=["POST"])
@mentor_required
def bulk_approve(aid):
    ach = Achievement.query.get_or_404(aid)
    cert = ach.certificate
    if cert and cert.verification_status == "Verified":
        ach.status = "Approved"
        ach.reviewed_by = current_user.id
        ach.reviewed_at = datetime.utcnow()
        db.session.commit()
        flash("Approved (verified certificate).", "success")
    else:
        flash("Only auto-approve verified certificates individually.", "warning")
    return redirect(url_for("mentor.submissions"))


@bp.route("/analytics")
@mentor_required
def analytics():
    return render_template("mentor/analytics.html")


@bp.route("/reports")
@mentor_required
def reports():
    return render_template("mentor/reports.html")


@bp.route("/messages", methods=["GET", "POST"])
@bp.route("/messages/<int:student_id>", methods=["GET", "POST"])
@mentor_required
def messages(student_id=None):
    current_user_id = int(current_user.get_id())
    current_user_name = current_user.full_name
    _mark_message_notifications_read()
    problem_filter = (request.args.get("problem_type") or "").strip()
    priority_filter = (request.args.get("priority") or "").strip()
    status_filter = (request.args.get("status") or "").strip()
    search = (request.args.get("q") or "").strip()
    student_ids = {
        row[0]
        for row in db.session.query(Message.sender_id)
        .filter(Message.receiver_id == current_user_id)
        .all()
    }
    student_ids.update(
        row[0]
        for row in db.session.query(Message.receiver_id)
        .filter(Message.sender_id == current_user_id)
        .all()
    )
    student_ids.update(
        row[0]
        for row in db.session.query(User.id).filter(User.role == "student").all()
    )
    student_ids.discard(current_user_id)

    conversation_rows = []
    if student_ids:
        students = (
            User.query.filter(User.id.in_(student_ids), User.role == "student")
            .order_by(User.full_name)
            .all()
        )
        for student in students:
            convo_messages = (
                Message.query.filter_by(conversation_id=_conversation_id(current_user_id, student.id))
                .order_by(Message.created_at.asc())
                .all()
            )
            row = {
                "student": student,
                "messages": convo_messages,
                "status": _conversation_status(convo_messages, current_user_id),
                "meta": _conversation_meta(convo_messages, current_user_id),
                "unread": _unread_notifications_for_student(student, current_user_id),
            }
            if problem_filter and row["meta"].get("problem_type") != problem_filter:
                continue
            if priority_filter and row["meta"].get("priority") != priority_filter:
                continue
            if status_filter and row["status"] != status_filter:
                continue
            if search:
                haystack = " ".join([
                    student.full_name or "",
                    student.email or "",
                    *(m.body or "" for m in convo_messages),
                ]).lower()
                if search.lower() not in haystack:
                    continue
            conversation_rows.append(row)

    selected_student = None
    if student_id:
        selected_student = User.query.filter_by(id=student_id, role="student").first_or_404()
    elif conversation_rows:
        selected_student = conversation_rows[0]["student"]

    if request.method == "POST":
        if not selected_student:
            flash("Please select a student conversation.", "danger")
            return redirect(url_for("mentor.messages"))
        body = (request.form.get("body") or "").strip()
        attachment_file = request.files.get("attachment")
        try:
            attachment_rel, _ = save_upload(attachment_file, "message_attachments")
        except RuntimeError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("mentor.messages", student_id=selected_student.id))
        if attachment_file and attachment_file.filename and not attachment_rel:
            flash("This file type is not supported for chat sharing.", "danger")
            return redirect(url_for("mentor.messages", student_id=selected_student.id))
        if not body and not attachment_rel:
            flash("Please write a reply or attach a note/material.", "danger")
            return redirect(url_for("mentor.messages", student_id=selected_student.id))
        if not body:
            body = "Shared a note or study material."
        msg = Message(
            sender_id=current_user_id,
            receiver_id=selected_student.id,
            body=_build_chat_body(body, attachment=attachment_rel),
            conversation_id=_conversation_id(current_user_id, selected_student.id),
        )
        Message.query.filter_by(
            conversation_id=_conversation_id(current_user_id, selected_student.id)
        ).update({"is_resolved": False})
        db.session.add(msg)
        db.session.add(
            Notification(
                user_id=selected_student.id,
                title="Mentor Reply",
                message=f"You got a reply from {current_user_name}.",
            )
        )
        db.session.commit()
        _send_chat_email(
            selected_student.email,
            f"Reply from {current_user_name}",
            (
                f"You got a reply from mentor {current_user_name}.\n"
                f"Open: {url_for('student.messages', _external=True)}\n\n{body}"
            ),
        )
        flash("Reply sent to student.", "success")
        return redirect(url_for("mentor.messages", student_id=selected_student.id))

    chat_messages = []
    if selected_student:
        chat_messages = (
            Message.query.filter_by(
                conversation_id=_conversation_id(current_user_id, selected_student.id)
            )
            .order_by(Message.created_at.asc())
            .all()
        )

    return render_template(
        "mentor/messages.html",
        conversation_rows=conversation_rows,
        selected_student=selected_student,
        messages=_message_rows(chat_messages),
        shared_materials=_shared_material_rows(chat_messages),
        selected_user_stats=_user_profile_stats(selected_student) if selected_student else None,
        problem_types=PROBLEM_TYPES,
        priorities=PRIORITIES,
        statuses=CONVERSATION_STATUSES,
        filters={
            "problem_type": problem_filter,
            "priority": priority_filter,
            "status": status_filter,
            "q": search,
        },
        conversation_status=_conversation_status(chat_messages, current_user_id) if chat_messages else "Open",
    )


@bp.route("/messages/<int:student_id>/status", methods=["POST"])
@mentor_required
def message_status(student_id):
    student = User.query.filter_by(id=student_id, role="student").first_or_404()
    resolved = request.form.get("status") == "Resolved"
    conversation = _conversation_id(int(current_user.get_id()), student.id)
    Message.query.filter_by(conversation_id=conversation).update({"is_resolved": resolved})
    db.session.commit()
    flash("Conversation marked as resolved." if resolved else "Conversation reopened.", "success")
    return redirect(url_for("mentor.messages", student_id=student.id))


@bp.route("/export/full-dataset")
@mentor_required
def export_full_dataset():
    output = export_civic_excel()
    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="civicvoice_citizen_complaints.xlsx",
    )


@bp.route("/export/excel")
@mentor_required
def export_all_excel():
    output = export_civic_excel()
    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="citizen_complaints.xlsx",
    )


@bp.route("/export/csv")
@mentor_required
def export_all_csv():
    output = export_civic_csv()
    return send_file(
        output,
        mimetype="text/csv",
        as_attachment=True,
        download_name="citizen_complaints.csv",
    )


@bp.route("/export/department-pdf")
@mentor_required
def export_department_pdf():
    dept = request.args.get("department", current_user.department or "All")
    complaints = Complaint.query.join(Complaint.department)
    if dept != "All":
        complaints = complaints.filter(Department.name == dept)
    pdf = civic_department_pdf(dept, complaints.order_by(Complaint.created_at.desc()).all())
    return send_file(
        pdf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"civicvoice_{dept.lower().replace(' ', '_')}_report.pdf",
    )


@bp.route("/leaderboard")
@mentor_required
def leaderboard():

    top = []

    for s in User.query.filter_by(role="student").all():

        # Skip Demo Student
        if s.email == "student@example.com":
            continue

        ach = Achievement.query.filter_by(
            student_id=s.id,
            status="Approved"
        ).all()

        pts = calculate_achievement_points(ach)

        top.append({
            "student": s,
            "points": pts,
            "count": len(ach)
        })

    top.sort(key=lambda x: x["points"], reverse=True)

    return render_template(
        "mentor/leaderboard.html",
        leaderboard=top
    )

def _filtered_submissions_query():
    status = request.args.get("status", "")
    category = request.args.get("category", "")
    search = request.args.get("q", "").strip()
    dept = request.args.get("department", "")
    year = request.args.get("year", "")
    q = Achievement.query.join(User, Achievement.student_id == User.id).filter(Achievement.status != "Draft")
    if status:
        q = q.filter(Achievement.status == status)
    if category:
        q = q.filter(Achievement.category == category)
    if search:
        q = q.filter(
            db.or_(
                Achievement.title.ilike(f"%{search}%"),
                User.full_name.ilike(f"%{search}%"),
            )
        )
    if dept:
        q = q.filter(db.or_(Achievement.branch == dept, User.department == dept))
    if year:
        q = q.filter(db.or_(Achievement.year == year, User.year == year))
    return q.order_by(Achievement.created_at.desc())


def _notify_students_for_classroom_post(post):
    students = User.query.filter_by(role="student")
    if post.branch:
        students = students.filter(User.department == post.branch)
    if post.year:
        students = students.filter(User.year == post.year)

    label = "New deadline" if post.post_type == "deadline" else "Upcoming event"
    when = post.due_at.strftime("%d %b %Y, %I:%M %p") if post.due_at else ""
    for student in students.all():
        db.session.add(
            Notification(
                user_id=student.id,
                title=label,
                message=f"{post.title} - {when}",
            )
        )

def _conversation_id(first_user_id, second_user_id):
    left, right = sorted([int(first_user_id), int(second_user_id)])
    return f"{left}:{right}"


def _build_chat_body(body, attachment=None):
    if not attachment:
        return body
    return f"Attachment: {attachment}\n\n{body}"


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


def _conversation_meta(messages, current_user_id):
    for message in messages:
        if message.sender_id != current_user_id:
            meta = _message_meta(message)
            return {
                "problem_type": meta.get("problem_type", "Other"),
                "priority": meta.get("priority", "Normal"),
                "subject": meta.get("subject", "Message"),
            }
    return {"problem_type": "Other", "priority": "Normal", "subject": "Message"}


def _conversation_status(messages, current_user_id):
    if messages and all(m.is_resolved for m in messages):
        return "Resolved"
    if any(m.sender_id == current_user_id for m in messages):
        return "In Progress"
    return "Open"


def _mark_message_notifications_read():
    current_user_id = int(current_user.get_id())
    q = Notification.query.filter(
        Notification.user_id == current_user_id,
        Notification.is_read.is_(False),
        Notification.title == "New Student Message",
    )
    if not q.first():
        return
    try:
        q.update({"is_read": True}, synchronize_session=False)
        db.session().expire_on_commit = False
        db.session.commit()
    except Exception:
        db.session.rollback()


def _unread_notifications_for_student(student, current_user_id):
    return Notification.query.filter(
        Notification.user_id == current_user_id,
        Notification.is_read.is_(False),
        Notification.title == "New Student Message",
        Notification.message.ilike(f"%{student.full_name}%"),
    ).count()


def _send_chat_email(recipient, subject, body):
    return send_plain_email(recipient, subject, body)


