import os

from dotenv import load_dotenv

load_dotenv()

from app import create_app, db

app = create_app()

MENTOR_EMAIL = "binaryai0010@gmail.com"
MENTOR_PASSWORD = "narayan22"


@app.cli.command("seed")
def seed_data():
    """Create or update the SAAMS mentor account."""
    from app.models import User

    mentor = User.query.filter_by(email=MENTOR_EMAIL).first()
    if not mentor:
        mentor = User.query.filter_by(role="mentor").first()
    if mentor:
        mentor.full_name = "ASHWINI MENTOR OF THE SAAMS , WELCOME TO THE SAAMS "
        mentor.email = MENTOR_EMAIL
        mentor.mobile = mentor.mobile or "9876543211"
        mentor.role = "mentor"
        mentor.department = mentor.department or "All Departments"
        mentor.employee_id = mentor.employee_id or "MTR001"
        mentor.is_verified = True
    else:
        mentor = User(
            full_name="ASHWINI MENTOR OF THE SAAMS , WELCOME TO THE SAAMS ",
            email=MENTOR_EMAIL,
            mobile="9876543211",
            role="mentor",
            department="All Departments",
            employee_id="MTR001",
            is_verified=True,
        )
        db.session.add(mentor)
    mentor.set_password(MENTOR_PASSWORD)

    for old in User.query.filter(User.role == "mentor", User.email != MENTOR_EMAIL).all():
        old.email = f"archived_{old.id}_{old.email}"[:120]
        old.role = "student"

    db.session.commit()
    print(f"Seed complete.")
    print(f"  Mentor: {MENTOR_EMAIL} / {MENTOR_PASSWORD}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        with app.app_context():
            seed_data()
    else:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=app.debug)
