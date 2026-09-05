import io
import json
from datetime import datetime
from xml.sax.saxutils import escape

import pandas as pd
from flask import current_app
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.models import Achievement, Activity, Certificate, Complaint, Department, User
from app.utils.helpers import calculate_achievement_points


def achievements_to_dataframe(achievements, include_student=False):
    rows = []
    for a in achievements:
        student = a.student if include_student else None
        cert = a.certificate
        row = {
            "Achievement Title": a.title,
            "Category": a.category,
            "Event Name": a.event_name or "",
            "Organizer": a.organizer or "",
            "Date": a.event_date.isoformat() if a.event_date else "",
            "Rank": a.rank or "",
            "Level": a.level or "",
            "Approval Status": a.status,
            "Verification Status": cert.verification_status if cert else "N/A",
            "Fraud Risk": cert.fraud_risk if cert else "N/A",
            "Name Match %": round((cert.match_score or 0) * 100, 1) if cert else "",
            "Authenticity %": round((cert.authenticity_score or 0) * 100, 1) if cert else "",
            "Certificate File": cert.file_name if cert else "",
            "Uploaded At": cert.uploaded_at.strftime("%Y-%m-%d %H:%M") if cert and cert.uploaded_at else "",
            "Mentor Remarks": a.mentor_comment or "",
        }
        if include_student and student:
            row = {
                "Student Name": student.full_name,
                "Email": student.email,
                "Roll Number": student.roll_number or "",
                "Department": student.department or "",
                "Year": student.year or "",
                **row,
            }
        rows.append(row)
    return pd.DataFrame(rows)


def certificates_to_dataframe():
    rows = []
    for c in Certificate.query.order_by(Certificate.uploaded_at.desc()).all():
        student = None
        title = ""
        if c.achievement:
            student = c.achievement.student
            title = c.achievement.title
        elif c.activity:
            student = c.activity.student
            title = c.activity.activity_name
        notes = []
        if c.fraud_notes:
            try:
                notes = json.loads(c.fraud_notes)
            except json.JSONDecodeError:
                notes = [c.fraud_notes]
        rows.append({
            "Student Name": student.full_name if student else "",
            "Email": student.email if student else "",
            "Department": student.department if student else "",
            "Linked To": title,
            "File Name": c.file_name or "",
            "Verification Status": c.verification_status or "",
            "Fraud Risk": c.fraud_risk or "",
            "Name Match %": round((c.match_score or 0) * 100, 1),
            "Confidence %": round((c.confidence_score or 0) * 100, 1),
            "Authenticity %": round((c.authenticity_score or 0) * 100, 1),
            "Detected Name": c.detected_name or "",
            "Detected Event": c.detected_event or "",
            "Scanner Notes": "; ".join(notes) if notes else "",
            "Uploaded At": c.uploaded_at.strftime("%Y-%m-%d %H:%M") if c.uploaded_at else "",
        })
    return pd.DataFrame(rows)


def students_summary_dataframe():
    rows = []
    for s in User.query.filter_by(role="student").order_by(User.full_name).all():
        achievements = Achievement.query.filter_by(student_id=s.id).all()
        activities = Activity.query.filter_by(student_id=s.id).all()
        certs = []
        seen = set()
        for a in achievements:
            if a.certificate and a.certificate.id not in seen:
                certs.append(a.certificate)
                seen.add(a.certificate.id)
        for act in activities:
            if act.certificate and act.certificate.id not in seen:
                certs.append(act.certificate)
                seen.add(act.certificate.id)
        rows.append({
            "Full Name": s.full_name,
            "Email": s.email,
            "Mobile": s.mobile or "",
            "Department": s.department or "",
            "Year": s.year or "",
            "Roll Number": s.roll_number or "",
            "Total Achievements": len(achievements),
            "Approved": len([a for a in achievements if a.status == "Approved"]),
            "Pending": len([a for a in achievements if a.status in ("Submitted", "Under Review")]),
            "Rejected": len([a for a in achievements if a.status == "Rejected"]),
            "Total Activities": len(activities),
            "Certificates Uploaded": len(certs),
            "Verified Certs": len([c for c in certs if c.verification_status == "Verified"]),
            "Suspected Fake": len([c for c in certs if c.verification_status == "Suspected Fake"]),
            "Achievement Points": calculate_achievement_points(achievements),
            "Registered": s.created_at.strftime("%Y-%m-%d") if s.created_at else "",
        })
    return pd.DataFrame(rows)


def export_excel(achievements, sheet_name="Achievements", include_student=False):
    df = achievements_to_dataframe(achievements, include_student=include_student)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)
    output.seek(0)
    return output


def export_comprehensive_excel():
    """Full dataset: students, achievements, certificates, summary stats."""
    achievements = Achievement.query.filter(Achievement.status != "Draft").all()
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        students_summary_dataframe().to_excel(writer, index=False, sheet_name="All Students")
        achievements_to_dataframe(achievements, include_student=True).to_excel(
            writer, index=False, sheet_name="Achievements History"
        )
        cert_df = certificates_to_dataframe()
        cert_df.to_excel(writer, index=False, sheet_name="Certificate Uploads")
        summary = pd.DataFrame([{
            "Total Students": User.query.filter_by(role="student").count(),
            "Total Certificates Uploaded": Certificate.query.count(),
            "Verified": Certificate.query.filter_by(verification_status="Verified").count(),
            "Suspected Fake": Certificate.query.filter_by(verification_status="Suspected Fake").count(),
            "Name Mismatch": Certificate.query.filter_by(verification_status="Name Mismatch").count(),
            "Manual Review": Certificate.query.filter_by(verification_status="Manual Review Required").count(),
            "Exported At": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        }])
        summary.to_excel(writer, index=False, sheet_name="Summary")
    output.seek(0)
    return output


def export_csv(achievements, include_student=False):
    df = achievements_to_dataframe(achievements, include_student=include_student)
    return io.BytesIO(df.to_csv(index=False).encode("utf-8"))


def citizen_complaints_dataframe(complaints=None):
    """Exportable, government-facing view of citizen profiles and complaints."""
    complaints = complaints if complaints is not None else Complaint.query.order_by(Complaint.created_at.desc()).all()
    rows = []
    for complaint in complaints:
        citizen = complaint.citizen
        department = complaint.department
        address = ", ".join(part for part in [complaint.address, complaint.city, complaint.state, complaint.pincode] if part)
        rows.append({
            "Tracking ID": complaint.tracking_id,
            "Citizen Name": citizen.full_name if citizen else "",
            "Citizen Email": citizen.email if citizen else "",
            "Citizen Phone": citizen.mobile if citizen else "",
            "Citizen Address": address,
            "Problem Title": complaint.title,
            "Description": complaint.description,
            "Category": complaint.category,
            "Priority": complaint.priority,
            "Status": complaint.status,
            "Department": department.name if department else "Unassigned",
            "Submitted At": complaint.created_at.strftime("%Y-%m-%d %H:%M UTC") if complaint.created_at else "",
            "Resolved At": complaint.resolved_at.strftime("%Y-%m-%d %H:%M UTC") if complaint.resolved_at else "",
        })
    return pd.DataFrame(rows)


def export_civic_excel(complaints=None):
    complaints = complaints if complaints is not None else Complaint.query.order_by(Complaint.created_at.desc()).all()
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        citizen_complaints_dataframe(complaints).to_excel(writer, index=False, sheet_name="Citizen Complaints")
        summary = pd.DataFrame([{
            "Total Citizens": User.query.filter(User.role.in_(["citizen", "student"])).count(),
            "Total Complaints": len(complaints),
            "Submitted": sum(item.status == "Submitted" for item in complaints),
            "In Progress": sum(item.status in {"Acknowledged", "Assigned", "In Progress"} for item in complaints),
            "Resolved": sum(item.status in {"Resolved", "Closed"} for item in complaints),
            "Exported At": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        }])
        summary.to_excel(writer, index=False, sheet_name="Summary")
    output.seek(0)
    return output


def export_civic_csv(complaints=None):
    return io.BytesIO(citizen_complaints_dataframe(complaints).to_csv(index=False).encode("utf-8-sig"))


def civic_department_pdf(department, complaints):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    active = sum(item.status not in {"Resolved", "Closed", "Rejected"} for item in complaints)
    story = [
        Paragraph(f"CivicVoice Department Report — {escape(department)}", styles["Title"]),
        Paragraph(f"Total complaints: {len(complaints)} | Active: {active} | Resolved: {sum(item.status in {'Resolved', 'Closed'} for item in complaints)}", styles["Normal"]),
        Spacer(1, 0.25 * inch),
    ]
    data = [["Tracking ID", "Citizen", "Problem", "Status"]]
    for item in complaints[:100]:
        data.append([item.tracking_id, _short(item.citizen.full_name if item.citizen else ""), _short(item.title), item.status])
    table = Table(data, repeatRows=1, colWidths=[100, 110, 190, 100])
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))
    story.append(table)
    doc.build(story)
    buffer.seek(0)
    return buffer


def _p(value):
    return escape(str(value or ""))


def _short(value, limit=80):
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "..."


def _achievement_date(achievement):
    return achievement.event_date or achievement.created_at


def _sort_datetime(value):
    if not value:
        return datetime.min
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, datetime.min.time())


def student_portfolio_pdf(student, achievements, activities, points, badges, portfolio_level=None):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=34,
        leftMargin=34,
        topMargin=34,
        bottomMargin=34,
        title=f"{student.full_name} Portfolio",
    )
    styles = getSampleStyleSheet()
    navy = colors.HexColor("#111827")
    ink = colors.HexColor("#1f2937")
    muted = colors.HexColor("#6b7280")
    cream = colors.HexColor("#fff7ed")
    line = colors.HexColor("#e5e7eb")
    accent = colors.HexColor((portfolio_level or {}).get("color", "#111827"))
    red = colors.HexColor("#991b1b")
    green = colors.HexColor("#065f46")

    styles.add(ParagraphStyle("PortfolioTitle", parent=styles["Heading1"], fontSize=26, leading=30, textColor=colors.white, spaceAfter=8))
    styles.add(ParagraphStyle("PortfolioSub", parent=styles["Normal"], fontSize=9.5, leading=13, textColor=colors.HexColor("#e5e7eb")))
    styles.add(ParagraphStyle("SectionTitle", parent=styles["Heading2"], fontSize=14, leading=18, textColor=navy, spaceBefore=12, spaceAfter=8))
    styles.add(ParagraphStyle("Tiny", parent=styles["Normal"], fontSize=7.5, leading=10, textColor=muted))
    styles.add(ParagraphStyle("CardLabel", parent=styles["Normal"], fontSize=8, leading=10, textColor=muted))
    styles.add(ParagraphStyle("CardValue", parent=styles["Heading2"], fontSize=16, leading=18, textColor=navy, spaceAfter=0))
    styles.add(ParagraphStyle("Badge", parent=styles["Normal"], fontSize=8.5, leading=11, textColor=navy))

    approved = sorted(
        [a for a in achievements if a.status == "Approved"],
        key=lambda a: _sort_datetime(_achievement_date(a)),
        reverse=True,
    )
    pending = [a for a in achievements if a.status in ("Submitted", "Under Review")]
    rejected = [a for a in achievements if a.status == "Rejected"]
    certs = [a.certificate for a in achievements if a.certificate]
    level = portfolio_level or {"name": "Portfolio Starter", "label": "Level 1", "next": "60 pts or 3 approved achievements", "color": "#111827"}

    story = []

    hero_left = [
        Paragraph("Skill Connect Portfolio", styles["PortfolioTitle"]),
        Paragraph(
            f"<b>{_p(student.full_name)}</b><br/>{_p(student.email)}<br/>"
            f"{_p(student.department or 'Department N/A')} | Year {_p(student.year or 'N/A')} | Roll {_p(student.roll_number or 'N/A')}",
            styles["PortfolioSub"],
        ),
    ]
    hero_right = [
        Paragraph(f"<b>{points}</b><br/>Achievement Points", ParagraphStyle("HeroPoints", parent=styles["Normal"], fontSize=20, leading=22, alignment=1, textColor=colors.white)),
        Spacer(1, 4),
        Paragraph(f"{_p(level['label'])}: {_p(level['name'])}", ParagraphStyle("HeroLevel", parent=styles["Normal"], fontSize=9, leading=11, alignment=1, textColor=colors.HexColor("#fde68a"))),
    ]
    hero = Table([[hero_left, hero_right]], colWidths=[4.8 * inch, 1.7 * inch])
    hero.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), navy),
        ("BOX", (0, 0), (-1, -1), 0, navy),
        ("LEFTPADDING", (0, 0), (-1, -1), 18),
        ("RIGHTPADDING", (0, 0), (-1, -1), 18),
        ("TOPPADDING", (0, 0), (-1, -1), 18),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 18),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(hero)
    story.append(Spacer(1, 0.18 * inch))

    summary_cards = [
        [Paragraph("Approved", styles["CardLabel"]), Paragraph(str(len(approved)), styles["CardValue"])],
        [Paragraph("Pending", styles["CardLabel"]), Paragraph(str(len(pending)), styles["CardValue"])],
        [Paragraph("Activities", styles["CardLabel"]), Paragraph(str(len(activities)), styles["CardValue"])],
        [Paragraph("Certificates", styles["CardLabel"]), Paragraph(str(len(certs)), styles["CardValue"])],
    ]
    card_table = Table([summary_cards], colWidths=[1.62 * inch] * 4)
    card_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), cream),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#fed7aa")),
        ("INNERGRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#fed7aa")),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(card_table)

    level_text = f"<b>{_p(level['name'])}</b>"
    if level.get("next"):
        level_text += f" - Next milestone: {_p(level['next'])}"
    level_table = Table([[Paragraph(level_text, styles["Normal"])]], colWidths=[6.5 * inch])
    level_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("BOX", (0, 0), (-1, -1), 1.2, accent),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(Spacer(1, 0.12 * inch))
    story.append(level_table)

    story.append(Paragraph("Badges", styles["SectionTitle"]))
    if badges:
        badge_cells = []
        for badge in badges[:12]:
            badge_cells.append(Paragraph(f"<b>{_p(badge)}</b>", styles["Badge"]))
        rows = [badge_cells[i:i + 3] for i in range(0, len(badge_cells), 3)]
        for row in rows:
            while len(row) < 3:
                row.append("")
        badge_table = Table(rows, colWidths=[2.1 * inch] * 3, hAlign="LEFT")
        badge_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eef2ff")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#c7d2fe")),
            ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.white),
            ("LEFTPADDING", (0, 0), (-1, -1), 9),
            ("RIGHTPADDING", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        story.append(badge_table)
    else:
        story.append(Paragraph("No badges yet. Approved submissions will unlock portfolio badges.", styles["Normal"]))

    story.append(Paragraph("Achievement Timeline", styles["SectionTitle"]))
    timeline_items = []
    for a in approved[:6]:
        date = _achievement_date(a)
        timeline_items.append([
            Paragraph(date.strftime("%d %b %Y") if date else "Date N/A", styles["Tiny"]),
            Paragraph(f"<b>{_p(_short(a.title, 70))}</b><br/><font color='#6b7280'>{_p(a.category)} | {_p(a.level or 'College')}</font>", styles["Normal"]),
        ])
    if timeline_items:
        timeline = Table(timeline_items, colWidths=[1.1 * inch, 5.25 * inch])
        timeline.setStyle(TableStyle([
            ("LINEBEFORE", (1, 0), (1, -1), 1.4, accent),
            ("LEFTPADDING", (1, 0), (1, -1), 14),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(timeline)
    else:
        story.append(Paragraph("No approved achievement timeline yet.", styles["Normal"]))

    story.append(Paragraph("Approved Achievements", styles["SectionTitle"]))

    if approved:
        data = [["Title", "Category", "Event", "Date", "Rank", "Level", "Certificate"]]
        for a in approved[:18]:
            data.append([
                Paragraph(_p(_short(a.title, 34)), styles["Tiny"]),
                Paragraph(_p(a.category), styles["Tiny"]),
                Paragraph(_p(_short(a.event_name, 28)), styles["Tiny"]),
                a.event_date.strftime("%Y-%m-%d") if a.event_date else "",
                Paragraph(_p(_short(a.rank, 18)), styles["Tiny"]),
                a.level or "",
                a.certificate.verification_status if a.certificate else "N/A",
            ])
        t = Table(data, colWidths=[1.25 * inch, 0.78 * inch, 1.1 * inch, 0.78 * inch, 0.72 * inch, 0.72 * inch, 1.05 * inch], repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), navy),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#ffffff")),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.35, line),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(t)
    else:
        story.append(Paragraph("No approved achievements yet.", styles["Normal"]))

    story.append(Paragraph("Activities & Participation", styles["SectionTitle"]))
    act_rows = [["Name", "Type", "Role", "Date", "Status"]]
    for act in activities[:14]:
        act_rows.append([
            Paragraph(_p(_short(act.activity_name, 36)), styles["Tiny"]),
            act.activity_type or "",
            Paragraph(_p(_short(act.role, 18)), styles["Tiny"]),
            act.date.strftime("%Y-%m-%d") if act.date else "",
            act.status,
        ])
    if len(act_rows) > 1:
        t2 = Table(act_rows, colWidths=[1.7 * inch, 1.1 * inch, 1.1 * inch, 1 * inch, 1 * inch], repeatRows=1)
        t2.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), accent),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.35, line),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")]),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(t2)
    else:
        story.append(Paragraph("No activities submitted yet.", styles["Normal"]))

    story.append(Paragraph("Certificate Verification Summary", styles["SectionTitle"]))
    if certs:
        verified = len([c for c in certs if c.verification_status in ("Verified", "Likely Authentic")])
        risky = len([c for c in certs if c.verification_status in ("Suspected Fake", "Name Mismatch") or c.fraud_risk in ("High", "Medium")])
        cert_summary = Table([
            [
                Paragraph("Uploaded Certificates", styles["CardLabel"]),
                Paragraph("Verified / Authentic", styles["CardLabel"]),
                Paragraph("Needs Attention", styles["CardLabel"]),
            ],
            [
                Paragraph(str(len(certs)), styles["CardValue"]),
                Paragraph(str(verified), styles["CardValue"]),
                Paragraph(str(risky), styles["CardValue"]),
            ],
        ], colWidths=[2.1 * inch] * 3)
        cert_summary.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
            ("GRID", (0, 0), (-1, -1), 0.45, line),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(cert_summary)
    else:
        story.append(Paragraph("No certificates uploaded yet.", styles["Normal"]))

    story.append(Spacer(1, 0.5 * inch))
    story.append(
        Paragraph(
            f"Generated by Skill Connect on {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} | This portfolio includes student-submitted and mentor-reviewed records.",
            styles["Tiny"],
        )
    )
    doc.build(story)
    buffer.seek(0)
    return buffer


def department_report_pdf(department, stats, top_students):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    story = [
        Paragraph(f"Department Report — {department}", styles["Title"]),
        Paragraph(f"Total Achievements: {stats.get('total', 0)}", styles["Normal"]),
        Paragraph(f"Approved: {stats.get('approved', 0)} | Pending: {stats.get('pending', 0)}", styles["Normal"]),
        Spacer(1, 0.3 * inch),
        Paragraph("<b>Top Students</b>", styles["Heading2"]),
    ]
    if top_students:
        data = [["Rank", "Name", "Points", "Approved"]]
        for i, s in enumerate(top_students, 1):
            data.append([str(i), s["name"], str(s["points"]), str(s["approved"])])
        t = Table(data)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey)]))
        story.append(t)
    doc.build(story)
    buffer.seek(0)
    return buffer
