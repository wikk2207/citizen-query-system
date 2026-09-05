import secrets
from io import BytesIO, StringIO
import csv
from datetime import datetime
from functools import wraps
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for, send_file
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename
from app import db
from app.models import Complaint, ComplaintAttachment, ComplaintFeedback, ComplaintStatusHistory, Department, Notification
from app.utils.helpers import save_upload

bp = Blueprint("civic", __name__)
CATEGORIES = {"Roads & Potholes":"Roads & Infrastructure", "Garbage & Sanitation":"Sanitation", "Water Supply":"Water Supply", "Street Lights":"Street Lighting", "Drainage":"Drainage", "Electricity":"Electricity", "Parks & Public Spaces":"Parks", "Public Safety":"Public Safety", "Other":"Municipal Corporation"}
def citizen_required(f):
    @wraps(f)
    @login_required
    def w(*a, **k):
        if current_user.role not in ("citizen", "student"): abort(403)
        return f(*a, **k)
    return w
def government_required(f):
    @wraps(f)
    @login_required
    def w(*a, **k):
        if current_user.role not in ("government", "mentor", "admin"): abort(403)
        return f(*a, **k)
    return w
def tracking_id():
    return f"CV-{datetime.utcnow().year}-{secrets.token_hex(4).upper()}"
def seed_departments():
    for i, name in enumerate(sorted(set(CATEGORIES.values())), 1):
        if not Department.query.filter_by(name=name).first(): db.session.add(Department(name=name, code=f"D{i:02d}"))
    db.session.commit()

@bp.route("/citizen/dashboard")
@citizen_required
def citizen_dashboard():
    complaints = Complaint.query.filter_by(citizen_id=current_user.id).order_by(Complaint.created_at.desc()).all()
    return render_template("civic/citizen_dashboard.html", complaints=complaints, points=len(complaints) * 10)

@bp.route("/citizen/report", methods=["GET", "POST"])
@citizen_required
def report_problem():
    seed_departments()
    if request.method == "POST":
        category = request.form.get("category", "Other"); dept = Department.query.filter_by(name=CATEGORIES.get(category, CATEGORIES["Other"])).first()
        c = Complaint(tracking_id=tracking_id(), citizen_id=current_user.id, title=request.form.get("title", "").strip(), description=request.form.get("description", "").strip(), category=category, priority=request.form.get("priority", "Normal"), department=dept, address=request.form.get("address"), city=request.form.get("city"), state=request.form.get("state"), pincode=request.form.get("pincode"), latitude=request.form.get("latitude", type=float), longitude=request.form.get("longitude", type=float))
        if not c.title or not c.description: flash("Title and description are required.", "danger"); return render_template("civic/report.html", categories=CATEGORIES)
        if not c.address and (c.latitude is None or c.longitude is None):
            db.session.rollback(); flash("Location is required. Use live location or enter a manual address.", "danger"); return render_template("civic/report.html", categories=CATEGORIES)
        db.session.add(c); db.session.flush()
        evidence = request.files.get("evidence")
        if evidence and evidence.filename:
            relative_path, stored_name = save_upload(evidence, "complaint_evidence")
            if not relative_path:
                db.session.rollback(); flash("This file type is not supported.", "danger"); return render_template("civic/report.html", categories=CATEGORIES)
            db.session.add(ComplaintAttachment(complaint_id=c.id, uploaded_by=current_user.id, file_name=stored_name, file_path=relative_path, attachment_type="citizen_evidence"))
        db.session.add(ComplaintStatusHistory(complaint_id=c.id, new_status="Submitted", changed_by=current_user.id)); db.session.add(Notification(user_id=current_user.id, title="Complaint submitted", message=f"Your complaint {c.tracking_id} was received.")); db.session.commit()
        return redirect(url_for("civic.complaint_detail", tracking_id=c.tracking_id))
    return render_template("civic/report.html", categories=CATEGORIES)

@bp.route("/citizen/complaints")
@citizen_required
def my_complaints(): return render_template("civic/complaints.html", complaints=Complaint.query.filter_by(citizen_id=current_user.id).order_by(Complaint.created_at.desc()).all())

@bp.route("/citizen/reports")
@citizen_required
def citizen_reports():
    return render_template("civic/reports.html", complaints=Complaint.query.filter_by(citizen_id=current_user.id).order_by(Complaint.created_at.desc()).all())

@bp.route("/citizen/analytics")
@citizen_required
def citizen_analytics():
    complaints = Complaint.query.filter_by(citizen_id=current_user.id).all()
    by_category = {}; by_status = {}; by_month = {}
    for c in complaints:
        by_category[c.category] = by_category.get(c.category, 0) + 1
        by_status[c.status] = by_status.get(c.status, 0) + 1
        month = c.created_at.strftime("%b %Y") if c.created_at else "Unknown"
        by_month[month] = by_month.get(month, 0) + 1
    return render_template("civic/citizen_analytics.html", by_category=by_category, by_status=by_status, by_month=by_month, total=len(complaints))

@bp.route("/citizen/reports.csv")
@citizen_required
def citizen_reports_csv():
    text_output = StringIO(); writer = csv.writer(text_output, lineterminator="\n")
    writer.writerow(["Tracking ID", "Title", "Category", "Status", "Priority", "Department", "City", "Created", "Resolved"])
    for c in Complaint.query.filter_by(citizen_id=current_user.id).order_by(Complaint.created_at.desc()):
        writer.writerow([c.tracking_id, c.title, c.category, c.status, c.priority, c.department.name if c.department else "", c.city or "", c.created_at, c.resolved_at or ""])
    output = BytesIO(text_output.getvalue().encode("utf-8-sig")); output.seek(0)
    return send_file(output, mimetype="text/csv", as_attachment=True, download_name="civicvoice-complaints.csv")

@bp.route("/complaints/<tracking_id>")
@login_required
def complaint_detail(tracking_id):
    c = Complaint.query.filter_by(tracking_id=tracking_id).first_or_404()
    if current_user.role in ("citizen", "student") and c.citizen_id != current_user.id: abort(403)
    return render_template("civic/detail.html", complaint=c, history=c.status_history.order_by(ComplaintStatusHistory.created_at).all())

@bp.route("/government/complaints")
@government_required
def government_queue(): return render_template("civic/queue.html", complaints=Complaint.query.order_by(Complaint.created_at.desc()).all())

@bp.route("/government/complaints/<tracking_id>/update", methods=["POST"])
@government_required
def update_complaint(tracking_id):
    c = Complaint.query.filter_by(tracking_id=tracking_id).first_or_404()
    new_status = (request.form.get("status") or c.status).strip(); note = (request.form.get("note") or "").strip()
    allowed = {"Submitted", "Acknowledged", "Assigned", "In Progress", "Needs Information", "On Hold", "Resolved", "Rejected", "Reopened", "Closed"}
    if new_status not in allowed or (new_status == "Rejected" and not note):
        flash("Choose a valid status and provide a reason when rejecting.", "danger"); return redirect(url_for("civic.complaint_detail", tracking_id=tracking_id))
    if new_status != c.status:
        old = c.status; c.status = new_status
        if new_status in ("Resolved", "Closed"): c.resolved_at = datetime.utcnow()
        db.session.add(ComplaintStatusHistory(complaint_id=c.id, previous_status=old, new_status=new_status, changed_by=current_user.id, note=note))
    if note: db.session.add(Notification(user_id=c.citizen_id, title="Complaint update", message=f"{c.tracking_id}: {note}"))
    db.session.commit(); flash("Complaint update sent to the citizen.", "success")
    return redirect(url_for("civic.complaint_detail", tracking_id=tracking_id))

@bp.route("/government/dashboard")
@government_required
def government_dashboard():
    complaints = Complaint.query.order_by(Complaint.created_at.desc()).all()
    stats = {
        "total": len(complaints),
        "new": sum(c.status == "Submitted" for c in complaints),
        "active": sum(c.status in ("Acknowledged", "Assigned", "In Progress") for c in complaints),
        "resolved": sum(c.status in ("Resolved", "Closed") for c in complaints),
        "overdue": 0,
    }
    return render_template("civic/government_dashboard.html", complaints=complaints, stats=stats)

@bp.route("/government/analytics")
@government_required
def analytics():
    complaints = Complaint.query.all()
    by_category = {}
    by_status = {}
    for c in complaints:
        by_category[c.category] = by_category.get(c.category, 0) + 1
        by_status[c.status] = by_status.get(c.status, 0) + 1
    return render_template("civic/analytics.html", total=len(complaints), by_category=by_category, by_status=by_status)

@bp.route("/track", methods=["GET", "POST"])
def track():
    c = Complaint.query.filter_by(tracking_id=request.values.get("tracking_id", "").strip().upper()).first() if request.values.get("tracking_id") else None
    return render_template("civic/track.html", complaint=c)
