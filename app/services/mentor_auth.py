"""Mentor authentication — whitelist, RBAC, credential checks."""
import os

from flask import session


def get_mentor_whitelist():
    raw = os.environ.get("MENTOR_WHITELIST_EMAILS", "").strip()
    if raw:
        return {e.strip().lower() for e in raw.split(",") if e.strip()}
    default = os.environ.get("MENTOR_EMAIL", "").strip().lower()
    return {default} if default else set()


def is_whitelisted_mentor_email(email):
    if not email:
        return False
    return email.strip().lower() in get_mentor_whitelist()


def validate_mentor_credentials(user, password):
    """Returns (ok, error_code, message)."""
    if not user:
        return False, "not_found", "No account found for this email."
    if user.role != "mentor":
        return False, "not_mentor", "This account is not authorized as a mentor."
    if not is_whitelisted_mentor_email(user.email):
        return False, "not_whitelisted", "Mentor access is restricted to approved faculty accounts."
    if not user.check_password(password):
        return False, "bad_password", "Incorrect password."
    return True, "ok", "Credentials valid."


def mentor_session_verified():
    return session.get("mentor_otp_verified") is True


def set_mentor_session_verified(user_id):
    session["mentor_otp_verified"] = True
    session["mentor_verified_user_id"] = user_id


def clear_mentor_session():
    session.pop("mentor_otp_verified", None)
    session.pop("mentor_verified_user_id", None)
    session.pop("mentor_pending_user_id", None)
