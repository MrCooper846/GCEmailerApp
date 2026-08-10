"""add company personalization

Revision ID: 7bc42d0e1a91
Revises: 2882d8b31510
Create Date: 2026-08-10 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "7bc42d0e1a91"
down_revision = "2882d8b31510"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("campaigns", schema=None) as batch_op:
        batch_op.add_column(sa.Column("company_column", sa.String(length=255), nullable=True))
    with op.batch_alter_table("campaign_recipients", schema=None) as batch_op:
        batch_op.add_column(sa.Column("recipient_company", sa.String(length=500), nullable=True))


def downgrade():
    with op.batch_alter_table("campaign_recipients", schema=None) as batch_op:
        batch_op.drop_column("recipient_company")
    with op.batch_alter_table("campaigns", schema=None) as batch_op:
        batch_op.drop_column("company_column")
