"""Move legacy application-startup column repairs into an explicit migration."""

from alembic import op
import sqlalchemy as sa


revision = "20260905_legacy_columns"
down_revision = "20260905_civic"
branch_labels = None
depends_on = None


def _add_missing_columns(table_name, columns):
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns(table_name)}
    for name, column_type in columns.items():
        if name not in existing:
            op.add_column(table_name, sa.Column(name, column_type, nullable=True))


def upgrade():
    _add_missing_columns("certificates", {
        "file_hash": sa.String(length=64),
        "fraud_risk": sa.String(length=20),
        "fraud_notes": sa.Text(),
    })
    identity_columns = {
        "branch": sa.String(length=80),
        "year": sa.String(length=20),
        "roll_number": sa.String(length=40),
    }
    _add_missing_columns("achievements", identity_columns)
    _add_missing_columns("activities", identity_columns)
    _add_missing_columns("users", {
        "preferred_language": sa.String(length=5),
        "address_line": sa.String(length=255),
        "locality": sa.String(length=120),
        "city": sa.String(length=120),
        "district": sa.String(length=120),
        "state": sa.String(length=120),
        "pincode": sa.String(length=12),
        "jurisdiction": sa.String(length=120),
        "office_location": sa.String(length=255),
        "mentor_designation": sa.String(length=120),
        "mentor_organization": sa.String(length=120),
        "mentor_experience_years": sa.String(length=40),
        "mentor_skills": sa.Text(),
        "mentor_bio": sa.Text(),
    })


def downgrade():
    # This revision intentionally has no downgrade: removing live profile data is unsafe.
    pass
