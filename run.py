import os

import click
from dotenv import load_dotenv

load_dotenv()

from app import create_app, db

app = create_app()


@app.cli.command("create-admin")
def create_admin():
    """Create the first administrator from environment variables, without logging secrets."""
    from app.models import User

    email = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD")
    name = (os.environ.get("ADMIN_NAME") or "CivicVoice Administrator").strip()
    if not email or not password:
        raise click.ClickException("ADMIN_EMAIL and ADMIN_PASSWORD must be set before creating an administrator.")
    if User.query.filter_by(email=email).first():
        raise click.ClickException("An account with ADMIN_EMAIL already exists; no changes were made.")

    admin = User(full_name=name, email=email, role="admin", is_verified=True)
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()
    click.echo("Administrator account created.")


@app.cli.command("init-db")
def init_db():
    """Create missing tables as an explicit operator action (never at web startup)."""
    db.create_all()
    click.echo("Database tables created or already present.")


if __name__ == "__main__":
    import sys

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=app.debug)
