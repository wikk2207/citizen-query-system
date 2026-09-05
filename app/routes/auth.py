from datetime import datetime



from flask import (

    Blueprint,

    current_app,

    flash,

    redirect,

    render_template,

    request,

    session,

    url_for,

)

from flask_login import current_user, login_required, login_user, logout_user



from app import db

from app.forms import (

    ForgotPasswordForm,

    LoginForm,

    OTPForm,

    OTPLoginForm,

    RegistrationForm,

    ResetPasswordForm,

)

from app.models import Notification, User, OTPCode

from app.services.otp_service import (

    create_otp,

    send_notification_email,

    send_otp_email,

    verify_otp as check_otp,

)

from app.utils.helpers import log_action, save_upload



bp = Blueprint("auth", __name__)





@bp.route("/register", methods=["GET", "POST"])

def register():

    if current_user.is_authenticated:

        return redirect(url_for("main.dashboard_redirect"))

    form = RegistrationForm()

    if form.validate_on_submit():

        if User.query.filter_by(email=form.email.data.lower()).first():

            flash("Email already registered. Try logging in.", "danger")

            return render_template("auth/register.html", form=form)

        try:

            user = User(

                full_name=form.full_name.data,

                email=form.email.data.lower(),

                mobile=form.mobile.data.strip(),

                role="citizen",

                department=form.department.data,

                preferred_language=form.preferred_language.data,
                address_line=form.address_line.data, locality=form.locality.data, city=form.city.data,
                district=form.district.data, state=form.state.data, pincode=form.pincode.data,

                employee_id=(form.employee_id.data or "").strip() or None,

                roll_number=(form.roll_number.data or "").strip() or None,

                is_verified=False,

            )

            user.set_password(form.password.data)

            if form.profile_photo.data and form.profile_photo.data.filename:

                try:

                    rel, _ = save_upload(form.profile_photo.data, "profiles")

                    user.profile_photo = rel

                except (ValueError, RuntimeError) as e:

                    flash(str(e), "warning")

            db.session.add(user)

            db.session.commit()

            code = create_otp(user.id, purpose="verification")

            session["pending_user_id"] = user.id

            session["otp_purpose"] = "verification"

            email_sent, otp_msg = send_otp_email(user, code, purpose="verification")

            flash(otp_msg, "success" if email_sent else "info")

            return redirect(url_for("auth.verify_otp"))

        except Exception as e:

            db.session.rollback()

            current_app.logger.exception("Registration failed")

            flash(f"Registration failed: {e}", "danger")

    elif request.method == "POST":

        flash("Please correct the errors below and try again.", "danger")

    return render_template("auth/register.html", form=form)





@bp.route("/verify-otp", methods=["GET", "POST"])

def verify_otp():

    user_id = session.get("pending_user_id")

    if not user_id:

        flash("No pending verification.", "warning")

        return redirect(url_for("auth.register"))

    user = User.query.get_or_404(user_id)

    form = OTPForm()

    dev_otp = session.get("dev_otp_code")

    if form.validate_on_submit():

        submitted_code = (form.code.data or "").strip()
        # In local development the code shown on this page is authoritative.
        # This avoids a stale OTP record winning after a resend.
        displayed_code = str(session.get("dev_otp_code") or "").strip()
        if displayed_code and submitted_code == displayed_code:
            otp = (OTPCode.query.filter_by(user_id=user.id, code=submitted_code, purpose="verification", is_used=False)
                   .order_by(OTPCode.expires_at.desc()).first())
            if otp and otp.expires_at >= datetime.utcnow():
                otp.is_used = True
                db.session.commit()
                ok, msg = True, "Verified"
            else:
                ok, msg = False, "OTP expired. Please resend the code."
        else:
            ok, msg = check_otp(user.id, submitted_code, purpose="verification")

        if ok:

            user.is_verified = True

            db.session.commit()

            session.pop("pending_user_id", None)

            session.pop("dev_otp_code", None)

            session.pop("otp_purpose", None)

            send_notification_email(

                user,

                "Welcome to Skill Connect",

                "emails/welcome.html",

            )

            db.session.add(Notification(user_id=user.id, title="Welcome", message="Account verified."))

            db.session.commit()

            login_user(user)

            session["celebrate"] = "register"

            flash("Account verified! Welcome to Skill Connect.", "success")

            return redirect(url_for("main.dashboard_redirect"))

        flash(msg, "danger")

    return render_template(

        "auth/verify_otp.html",

        form=form,

        email=user.email,

        dev_otp=dev_otp,

        mail_configured=bool(current_app.config.get("MAIL_USERNAME")),

        verify_mode="register",

    )





@bp.route("/login", methods=["GET", "POST"])
@bp.route("/citizen-login", methods=["GET", "POST"])

def login():

    """Login via email OTP — enter email, receive code, verify to sign in."""

    if current_user.is_authenticated:

        return redirect(url_for("main.dashboard_redirect"))

    form = OTPLoginForm()

    if form.validate_on_submit():

        user = User.query.filter_by(email=form.email.data.lower()).first()

        if not user:

            flash("No account found with this email. Please register first.", "danger")

            return render_template("auth/login.html", form=form)

        code = create_otp(user.id, purpose="login")

        session["otp_login_user_id"] = user.id

        session["otp_purpose"] = "login"

        email_sent, otp_msg = send_otp_email(user, code, purpose="login")

        flash(otp_msg, "success" if email_sent else "info")

        return redirect(url_for("auth.verify_login_otp"))

    return render_template("auth/login.html", form=form)





@bp.route("/mentor-login", methods=["GET", "POST"])
@bp.route("/government-login", methods=["GET", "POST"])
def mentor_login():
    """Mentor: email + password, then OTP (whitelist + RBAC)."""
    from app.services.mentor_auth import clear_mentor_session, validate_mentor_credentials

    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard_redirect"))

    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data.lower()).first()
        ok, code, msg = validate_mentor_credentials(user, form.password.data)
        if not ok:
            flash(msg, "danger")
            if code == "not_mentor":
                return redirect(url_for("main.access_denied"))
            return render_template("auth/mentor_login.html", form=form)

        clear_mentor_session()
        otp_code = create_otp(user.id, purpose="mentor_login")
        session["mentor_pending_user_id"] = user.id
        session["otp_purpose"] = "mentor_login"
        session["mentor_remember"] = form.remember.data
        email_sent, otp_msg = send_otp_email(user, otp_code, purpose="mentor_login")
        flash(otp_msg, "success" if email_sent else "info")
        return redirect(url_for("auth.mentor_verify_otp"))

    return render_template("auth/mentor_login.html", form=form)


@bp.route("/mentor-verify-otp", methods=["GET", "POST"])
def mentor_verify_otp():
    from app.services.mentor_auth import (
        clear_mentor_session,
        is_whitelisted_mentor_email,
        set_mentor_session_verified,
        validate_mentor_credentials,
    )

    user_id = session.get("mentor_pending_user_id")
    if not user_id:
        return redirect(url_for("auth.mentor_login"))

    user = User.query.get_or_404(user_id)
    if user.role != "mentor" or not is_whitelisted_mentor_email(user.email):
        clear_mentor_session()
        flash("Mentor access denied.", "danger")
        return redirect(url_for("main.access_denied"))

    form = OTPForm()
    dev_otp = session.get("dev_otp_code")
    if form.validate_on_submit():
        ok, msg = check_otp(user.id, form.code.data, purpose="mentor_login")
        if ok:
            set_mentor_session_verified(user.id)
            session.pop("mentor_pending_user_id", None)
            session.pop("dev_otp_code", None)
            session.pop("otp_purpose", None)
            remember = session.pop("mentor_remember", False)
            login_user(user, remember=remember)
            session.permanent = True
            session["celebrate"] = "login"
            log_action("mentor_login_otp", user.email)
            flash(f"Welcome, {user.full_name}!", "success")
            return redirect(url_for("mentor.dashboard"))
        flash(msg, "danger")

    return render_template(
        "auth/verify_otp.html",
        form=form,
        email=user.email,
        dev_otp=dev_otp,
        mail_configured=bool(current_app.config.get("MAIL_USERNAME")),
        verify_mode="mentor",
    )





@bp.route("/login-otp", methods=["GET", "POST"])

def login_otp():

    """Legacy URL — redirect to main OTP login."""

    return redirect(url_for("auth.login"))





@bp.route("/verify-login-otp", methods=["GET", "POST"])

def verify_login_otp():

    user_id = session.get("otp_login_user_id")

    if not user_id:

        return redirect(url_for("auth.login"))

    user = User.query.get_or_404(user_id)

    form = OTPForm()

    dev_otp = session.get("dev_otp_code")

    if form.validate_on_submit():

        ok, msg = check_otp(user.id, form.code.data, purpose="login")

        if ok:

            user.is_verified = True

            db.session.commit()

            session.pop("otp_login_user_id", None)

            session.pop("dev_otp_code", None)

            session.pop("otp_purpose", None)

            login_user(user)

            session.permanent = True

            session["celebrate"] = "login"

            log_action("login_otp", user.email)

            flash(f"Welcome back, {user.full_name}!", "success")

            return redirect(url_for("main.dashboard_redirect"))

        flash(msg, "danger")

    return render_template(

        "auth/verify_otp.html",

        form=form,

        email=user.email,

        dev_otp=dev_otp,

        mail_configured=bool(current_app.config.get("MAIL_USERNAME")),

        verify_mode="login",

    )





@bp.route("/resend-otp", methods=["POST"])

def resend_otp():

    user_id = (
        session.get("pending_user_id")
        or session.get("otp_login_user_id")
        or session.get("mentor_pending_user_id")
    )

    purpose = session.get("otp_purpose", "verification")

    if not user_id:

        flash("No pending verification. Please register or log in again.", "warning")

        return redirect(url_for("auth.register"))

    user = User.query.get_or_404(user_id)

    code = create_otp(user.id, purpose=purpose)

    email_sent, otp_msg = send_otp_email(user, code, purpose=purpose)

    flash(otp_msg, "success" if email_sent else "info")

    if session.get("mentor_pending_user_id"):
        return redirect(url_for("auth.mentor_verify_otp"))
    if session.get("otp_login_user_id"):
        return redirect(url_for("auth.verify_login_otp"))

    return redirect(url_for("auth.verify_otp"))





@bp.route("/forgot-password", methods=["GET", "POST"])

def forgot_password():

    form = ForgotPasswordForm()

    if form.validate_on_submit():

        user = User.query.filter_by(email=form.email.data.lower()).first()

        if user:

            code = create_otp(user.id, purpose="reset")

            send_otp_email(user, code, purpose="reset")

            session["reset_user_id"] = user.id

            flash("Reset OTP sent to your email.", "info")

            return redirect(url_for("auth.reset_password"))

        flash("If that email exists, an OTP was sent.", "info")

    return render_template("auth/forgot_password.html", form=form)





@bp.route("/reset-password", methods=["GET", "POST"])

def reset_password():

    user_id = session.get("reset_user_id")

    if not user_id:

        return redirect(url_for("auth.forgot_password"))

    user = User.query.get_or_404(user_id)

    form = ResetPasswordForm()

    if request.method == "POST" and request.form.get("code"):

        ok, msg = check_otp(user.id, request.form.get("code"), purpose="reset")

        if not ok:

            flash(msg, "danger")

            return render_template("auth/reset_password.html", form=form)

    if form.validate_on_submit():

        if request.form.get("code"):

            ok, _ = check_otp(user.id, request.form.get("code"), purpose="reset")

            if not ok:

                flash("Verify OTP first.", "danger")

                return render_template("auth/reset_password.html", form=form)

        user.set_password(form.password.data)

        db.session.commit()

        session.pop("reset_user_id", None)

        flash("Password updated. Please login with OTP.", "success")

        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", form=form)





@bp.route("/logout")

@login_required

def logout():
    from app.services.mentor_auth import clear_mentor_session

    clear_mentor_session()
    log_action("logout")

    logout_user()

    flash("Logged out successfully.", "info")

    return redirect(url_for("main.index"))





@bp.route("/profile", methods=["GET", "POST"])

@login_required

def profile():

    from app.forms import ProfileForm



    form = ProfileForm(obj=current_user)

    if form.validate_on_submit():

        current_user.full_name = form.full_name.data

        current_user.mentor_skills = form.mentor_skills.data
        current_user.mentor_bio = form.mentor_bio.data

        if current_user.is_mentor:
            current_user.mentor_designation = form.mentor_designation.data
            current_user.mentor_organization = form.mentor_organization.data
            current_user.mentor_experience_years = form.mentor_experience_years.data
        if current_user.role in ("government", "mentor", "admin"):
            current_user.employee_id = form.employee_id.data
            current_user.department = form.department.data
            current_user.jurisdiction = form.jurisdiction.data
            current_user.office_location = form.office_location.data
        else:
            current_user.mobile = form.mobile.data
            current_user.preferred_language = form.preferred_language.data or current_user.preferred_language
            current_user.address_line = form.address_line.data
            current_user.locality = form.locality.data; current_user.city = form.city.data
            current_user.district = form.district.data; current_user.state = form.state.data; current_user.pincode = form.pincode.data


        # Only update profile photo if a new file is uploaded
        if form.profile_photo.data and getattr(form.profile_photo.data, 'filename', None):
            if form.profile_photo.data.filename:
                try:
                    rel, _ = save_upload(form.profile_photo.data, "profiles")
                    current_user.profile_photo = rel
                except (ValueError, RuntimeError) as e:
                    flash(str(e), "warning")

        db.session.commit()

        flash("Profile updated.", "success")

        return redirect(url_for("auth.profile"))

    return render_template("auth/profile.html", form=form)


