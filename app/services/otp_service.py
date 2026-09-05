import random
import smtplib
import string
import os
from datetime import datetime, timedelta
from email.mime.text import MIMEText

from flask import current_app, render_template, session
from flask_mail import Message
import requests

from app import db, mail
from app.models import OTPCode


def generate_otp(length=6):
    return "".join(random.choices(string.digits, k=length))


def is_mail_configured():
    return bool(
        current_app.config.get("RESEND_API_KEY")
        or (
            current_app.config.get("MAIL_USERNAME")
            and current_app.config.get("MAIL_PASSWORD")
        )
    )


def store_dev_otp(code):
    """Keep OTP visible on verify page when email is not configured."""
    session["dev_otp_code"] = code
    session["dev_otp_expires"] = (datetime.utcnow() + timedelta(minutes=10)).isoformat()


def create_otp(user_id, purpose="verification", minutes=10):
    code = generate_otp()
    otp = OTPCode(
        user_id=user_id,
        code=code,
        expires_at=datetime.utcnow() + timedelta(minutes=minutes),
        purpose=purpose,
        is_used=False,
    )
    db.session.add(otp)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return code



def verify_otp(user_id, code, purpose="verification"):
    otp = (
        OTPCode.query.filter_by(
            user_id=user_id, code=code, purpose=purpose, is_used=False
        )
        .order_by(OTPCode.expires_at.desc())
        .first()
    )
    if not otp:
        return False, "Invalid OTP"
    if otp.expires_at < datetime.utcnow():
        return False, "OTP expired"
    otp.is_used = True
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return True, "Verified"



def send_otp_email(user, code, purpose="verification"):
    """
    Send OTP by email when SMTP is configured.
    Otherwise store in session for on-screen display (development).
    Returns: (email_sent: bool, message: str)
    """
    if current_app.config.get("DEV_OTP_MODE"):
        store_dev_otp(code)
        return False, "Your verification code is shown below (valid for 10 minutes)."

    subject = "CivicVoice Verification Code"
    if purpose == "login":
        subject = "Skill Connect Login OTP"
    elif purpose == "reset":
        subject = "Skill Connect Password Reset OTP"
    elif purpose == "mentor_login":
        subject = "Skill Connect Mentor Security OTP"

    if not is_mail_configured():
        if current_app.config.get("IS_PRODUCTION"):
            return False, "Email delivery is not configured. Please contact the administrator."
        store_dev_otp(code)
        current_app.logger.info("Development OTP generated for %s", user.email)
        return False, (
            f"Email is not configured. Your verification code is: {code} "
            "(also shown below — valid 10 minutes)"
        )

    try:
        body = render_template(
            "emails/otp.html",
            user=user,
            code=code,
            purpose=purpose,
        )
        sender = current_app.config.get("MAIL_DEFAULT_SENDER") or current_app.config.get("MAIL_USERNAME")
        if sender and "<" not in sender and "@" in sender:
            sender = f"Skill Connect <{sender}>"
        _send_email(
            user.email.strip(),
            subject,
            html_body=body,
            text_body=f"Your Skill Connect OTP is {code}. It is valid for 10 minutes.",
            sender=sender,
        )
        session.pop("dev_otp_code", None)
        current_app.logger.info("OTP email sent to %s", user.email)
        return True, f"OTP sent to {user.email}. Check your inbox and spam folder."
    except Exception as e:
        current_app.logger.error("Mail send failed: %s", e)
        store_dev_otp(code)
        return False, (
            f"Email could not be sent. Use this code: {code} "
            "(valid 10 minutes)"
        )


def send_notification_email(user, subject, template, **kwargs):
    if not is_mail_configured():
        current_app.logger.info("DEV email to %s: %s", user.email, subject)
        return True
    try:
        body = render_template(template, user=user, **kwargs)
        sender = current_app.config.get("MAIL_DEFAULT_SENDER") or current_app.config.get("MAIL_USERNAME")
        if sender and "<" not in sender and "@" in sender:
            sender = f"Skill Connect <{sender}>"
        _send_email(user.email, subject, html_body=body, sender=sender)
        return True
    except Exception as e:
        current_app.logger.error("Notification email failed: %s", e)
        return False


def send_plain_email(recipient, subject, body):
    if not recipient:
        return False
    if not is_mail_configured():
        current_app.logger.info("DEV email to %s: %s", recipient, subject)
        return True
    try:
        _send_email(recipient, subject, text_body=body)
        return True
    except Exception as exc:
        current_app.logger.error("Plain email failed: %s", exc)
        return False


def _send_email(recipient, subject, html_body=None, text_body=None, sender=None):
    if current_app.config.get("RESEND_API_KEY"):
        return _send_resend_email(
            recipient,
            subject,
            html_body=html_body,
            text_body=text_body,
        )

    if os.environ.get("RENDER") and not current_app.config.get("FORCE_SMTP"):
        raise RuntimeError(
            "SMTP is disabled on Render because Gmail SMTP is unreachable there. "
            "Set RESEND_API_KEY for real email delivery."
        )

    sender = sender or current_app.config.get("MAIL_DEFAULT_SENDER") or current_app.config.get("MAIL_USERNAME")
    if sender and "<" not in sender and "@" in sender:
        sender = f"Skill Connect <{sender}>"
    msg = Message(
        subject=subject,
        recipients=[recipient],
        html=html_body,
        body=text_body,
        sender=sender,
    )
    try:
        mail.send(msg)
    except Exception as smtp_error:
        if _network_unreachable(smtp_error):
            raise
        _send_direct_smtp(recipient, subject, html_body or text_body or "", sender)


def _send_resend_email(recipient, subject, html_body=None, text_body=None):
    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {current_app.config['RESEND_API_KEY']}",
            "Content-Type": "application/json",
        },
        json={
            "from": current_app.config.get("RESEND_FROM_EMAIL"),
            "to": [recipient],
            "subject": subject,
            "html": html_body or f"<pre>{text_body or ''}</pre>",
            "text": text_body or "",
        },
        timeout=int(current_app.config.get("MAIL_TIMEOUT", 6)),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Resend email failed: {response.status_code} {response.text[:300]}")


def _send_direct_smtp(recipient, subject, html_body, sender):
    host = current_app.config.get("MAIL_SERVER")
    port = int(current_app.config.get("MAIL_PORT", 587))
    username = current_app.config.get("MAIL_USERNAME")
    password = current_app.config.get("MAIL_PASSWORD")
    timeout = int(current_app.config.get("MAIL_TIMEOUT", 20))
    use_ssl = bool(current_app.config.get("MAIL_USE_SSL"))
    use_tls = bool(current_app.config.get("MAIL_USE_TLS"))

    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender or username
    msg["To"] = recipient

    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=timeout) as smtp:
            smtp.login(username, password)
            smtp.sendmail(sender or username, [recipient], msg.as_string())
        return

    with smtplib.SMTP(host, port, timeout=timeout) as smtp:
        smtp.ehlo()
        if use_tls:
            smtp.starttls()
            smtp.ehlo()
        smtp.login(username, password)
        smtp.sendmail(sender or username, [recipient], msg.as_string())


def _network_unreachable(exc):
    text = str(exc).lower()
    return (
        "network is unreachable" in text
        or "errno 101" in text
        or "errno 111" in text
        or "connection refused" in text
    )
