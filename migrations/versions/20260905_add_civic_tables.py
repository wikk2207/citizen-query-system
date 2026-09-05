"""add CivicVoice complaint domain
Revision ID: 20260905_civic
"""
from alembic import op
import sqlalchemy as sa

revision = "20260905_civic"
down_revision = "20260530_chat_message"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("departments", sa.Column("id", sa.Integer, primary_key=True), sa.Column("name", sa.String(120), nullable=False, unique=True), sa.Column("code", sa.String(30), nullable=False, unique=True), sa.Column("description", sa.Text), sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()), sa.Column("created_at", sa.DateTime))
    op.create_table("complaints", sa.Column("id", sa.Integer, primary_key=True), sa.Column("tracking_id", sa.String(32), nullable=False, unique=True), sa.Column("citizen_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False), sa.Column("title", sa.String(200), nullable=False), sa.Column("description", sa.Text, nullable=False), sa.Column("category", sa.String(80), nullable=False), sa.Column("subcategory", sa.String(80)), sa.Column("priority", sa.String(20), nullable=False), sa.Column("status", sa.String(30), nullable=False), sa.Column("department_id", sa.Integer, sa.ForeignKey("departments.id")), sa.Column("assigned_officer_id", sa.Integer, sa.ForeignKey("users.id")), sa.Column("address", sa.String(255)), sa.Column("locality", sa.String(120)), sa.Column("city", sa.String(120)), sa.Column("district", sa.String(120)), sa.Column("state", sa.String(120)), sa.Column("pincode", sa.String(12)), sa.Column("latitude", sa.Float), sa.Column("longitude", sa.Float), sa.Column("created_at", sa.DateTime), sa.Column("updated_at", sa.DateTime), sa.Column("resolved_at", sa.DateTime), sa.Column("resolution_summary", sa.Text))
    op.create_table("complaint_attachments", sa.Column("id", sa.Integer, primary_key=True), sa.Column("complaint_id", sa.Integer, sa.ForeignKey("complaints.id"), nullable=False), sa.Column("uploaded_by", sa.Integer, sa.ForeignKey("users.id"), nullable=False), sa.Column("file_name", sa.String(255), nullable=False), sa.Column("file_path", sa.String(500), nullable=False), sa.Column("attachment_type", sa.String(40)), sa.Column("created_at", sa.DateTime))
    op.create_table("complaint_status_history", sa.Column("id", sa.Integer, primary_key=True), sa.Column("complaint_id", sa.Integer, sa.ForeignKey("complaints.id"), nullable=False), sa.Column("previous_status", sa.String(30)), sa.Column("new_status", sa.String(30), nullable=False), sa.Column("changed_by", sa.Integer, sa.ForeignKey("users.id")), sa.Column("note", sa.Text), sa.Column("created_at", sa.DateTime))
    op.create_table("complaint_feedback", sa.Column("id", sa.Integer, primary_key=True), sa.Column("complaint_id", sa.Integer, sa.ForeignKey("complaints.id"), nullable=False, unique=True), sa.Column("citizen_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False), sa.Column("rating", sa.Integer, nullable=False), sa.Column("comment", sa.Text), sa.Column("created_at", sa.DateTime))
def downgrade():
    for t in ("complaint_feedback", "complaint_status_history", "complaint_attachments", "complaints", "departments"): op.drop_table(t)
