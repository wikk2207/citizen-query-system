import os
import tempfile

from dotenv import load_dotenv

load_dotenv()

from flask import Flask
from flask_cors import CORS
from flask_login import LoginManager
from flask_mail import Mail
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from werkzeug.exceptions import HTTPException

try:
    # When running as a package (recommended)
    from saams.config import config_map
except Exception:
    # When running from repo root / legacy entrypoints
    from config import config_map


db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
mail = Mail()
csrf = CSRFProtect()


def create_app(config_name=None):
    runtime_root = os.environ.get(
        "RUNTIME_DATA_DIR",
        os.path.join(tempfile.gettempdir(), "civicvoice") if os.environ.get("VERCEL") else os.path.join(os.path.dirname(os.path.dirname(__file__)), "instance"),
    )
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
        instance_path=os.path.abspath(runtime_root),
    )
    selected_config = config_name or os.environ.get("FLASK_ENV") or os.environ.get("FLASK_CONFIG") or ("production" if os.environ.get("VERCEL") else "default")
    config_class = config_map.get(selected_config, config_map["default"])
    app.config.from_object(config_class)
    app.config["IS_PRODUCTION"] = selected_config == "production" or bool(os.environ.get("VERCEL"))
    app.config["SESSION_COOKIE_SECURE"] = app.config["IS_PRODUCTION"]

    if app.config["IS_PRODUCTION"] and not app.config.get("SECRET_KEY"):
        raise RuntimeError("SECRET_KEY must be configured in the production environment.")
    if not app.config.get("SQLALCHEMY_DATABASE_URI"):
        raise RuntimeError("DATABASE_URL must be configured. SQLite and localhost fallbacks are not supported.")

    # Only create runtime directories. On Vercel this resolves to /tmp, never /var/task.
    os.makedirs(app.instance_path, exist_ok=True)
    if app.config["PERSISTENT_UPLOADS_ENABLED"]:
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
        os.makedirs(os.path.join(app.config["UPLOAD_FOLDER"], "profiles"), exist_ok=True)
        os.makedirs(os.path.join(app.config["UPLOAD_FOLDER"], "certificates"), exist_ok=True)

    db.init_app(app)

    # Enable WAL mode for SQLite to reduce write-lock contention.
    # This is safe to run on each startup.
    if app.config.get("SQLALCHEMY_DATABASE_URI", "").startswith("sqlite"):
        with app.app_context():
            try:
                with db.engine.begin() as conn:
                    conn.exec_driver_sql("PRAGMA journal_mode=WAL")
                    conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
            except Exception:
                # Don't block app boot if PRAGMAs fail for any reason.
                pass

    migrate.init_app(app, db)
    login_manager.init_app(app)
    mail.init_app(app)
    csrf.init_app(app)
    if app.config["CORS_ORIGINS"]:
        CORS(app, supports_credentials=True, origins=app.config["CORS_ORIGINS"])
    app.config["LAST_ERROR"] = None

    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "info"

    from app.routes import auth, main, mentor, student, api, voice, cert_dashboard, sara_api, civic
    from app.services.sara_voice import VoiceSession  # noqa: F401

    app.register_blueprint(main.bp)
    app.register_blueprint(auth.bp, url_prefix="/auth")
    app.register_blueprint(student.bp, url_prefix="/student")
    app.register_blueprint(mentor.bp, url_prefix="/mentor")
    app.register_blueprint(api.bp, url_prefix="/api")
    app.register_blueprint(voice.bp, url_prefix="/voice")
    app.register_blueprint(cert_dashboard.bp)
    app.register_blueprint(sara_api.bp)
    app.register_blueprint(civic.bp)

    @app.context_processor
    def inject_globals():
        from flask_login import current_user
        from app.models import Notification
        from app.services.otp_service import is_mail_configured

        unread = 0
        unread_messages = 0
        if current_user.is_authenticated:
            unread = Notification.query.filter_by(
                user_id=current_user.id, is_read=False
            ).count()
            unread_messages = Notification.query.filter(
                Notification.user_id == current_user.id,
                Notification.is_read.is_(False),
                Notification.title.in_(["New Student Message", "Mentor Reply"]),
            ).count()
        from flask import session
        try:
            from app.i18n import translate
        except ModuleNotFoundError:
            # Keep existing authentication pages renderable during legacy reloads.
            translate = lambda text, language="en": text
        language = (getattr(current_user, "preferred_language", None) if current_user.is_authenticated else None) or session.get("language", "en")
        return dict(
            unread_notifications=unread,
            unread_message_notifications=unread_messages,
            mail_configured=is_mail_configured(),
            language=language, t=lambda text: translate(text, language),
        )

    if app.config["AUTO_SCHEMA_MANAGEMENT"]:
        with app.app_context():
            db.create_all()
            _ensure_schema_columns(app)

    @app.errorhandler(Exception)
    def capture_unhandled_error(error):
        if isinstance(error, HTTPException):
            return error
        app.logger.exception("Unhandled application error")
        error_record = {"type": type(error).__name__}
        app.config["LAST_ERROR"] = error_record
        return "Internal Server Error", 500

    return app


def _ensure_schema_columns(app):
    """Add compatible columns to databases created before newer models."""
    from sqlalchemy import inspect, text

    with app.app_context():
        insp = inspect(db.engine)
        alters = []
        tables = set(insp.get_table_names())

        if "certificates" in tables:
            certificate_cols = {
                column["name"] for column in insp.get_columns("certificates")
            }
            if "file_hash" not in certificate_cols:
                alters.append("ALTER TABLE certificates ADD COLUMN file_hash VARCHAR(64)")
            if "fraud_risk" not in certificate_cols:
                alters.append("ALTER TABLE certificates ADD COLUMN fraud_risk VARCHAR(20)")
            if "fraud_notes" not in certificate_cols:
                alters.append("ALTER TABLE certificates ADD COLUMN fraud_notes TEXT")

        submission_identity_columns = {
            "branch": "VARCHAR(80)",
            "year": "VARCHAR(20)",
            "roll_number": "VARCHAR(40)",
        }
        for table_name in ("achievements", "activities"):
            if table_name in tables:
                cols = {column["name"] for column in insp.get_columns(table_name)}
                for name, column_type in submission_identity_columns.items():
                    if name not in cols:
                        alters.append(
                            f"ALTER TABLE {table_name} ADD COLUMN {name} {column_type}"
                        )

        if "users" in tables:
            user_cols = {column["name"] for column in insp.get_columns("users")}
            civic_user_columns = {
                "preferred_language": "VARCHAR(5)", "address_line": "VARCHAR(255)",
                "locality": "VARCHAR(120)", "city": "VARCHAR(120)",
                "district": "VARCHAR(120)", "state": "VARCHAR(120)", "pincode": "VARCHAR(12)",
                "jurisdiction": "VARCHAR(120)", "office_location": "VARCHAR(255)",
            }
            for name, column_type in civic_user_columns.items():
                if name not in user_cols:
                    alters.append(f"ALTER TABLE users ADD COLUMN {name} {column_type}")
            mentor_columns = {
                "mentor_designation": "VARCHAR(120)",
                "mentor_organization": "VARCHAR(120)",
                "mentor_experience_years": "VARCHAR(40)",
                "mentor_skills": "TEXT",
                "mentor_bio": "TEXT",
            }
            for name, column_type in mentor_columns.items():
                if name not in user_cols:
                    alters.append(
                        f"ALTER TABLE users ADD COLUMN {name} {column_type}"
                    )

        if alters:
            with db.engine.begin() as conn:
                for sql in alters:
                    conn.execute(text(sql))
