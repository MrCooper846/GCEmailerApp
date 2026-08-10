"""Persistent production data model for GC Emailer."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from extensions import db


def utcnow():
    return datetime.now(timezone.utc)


def uuid_string():
    return str(uuid.uuid4())


class TimestampMixin:
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class User(TimestampMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.String(36), primary_key=True, default=uuid_string)
    email = db.Column(db.String(320), nullable=False, unique=True, index=True)
    display_name = db.Column(db.String(200))
    role = db.Column(db.String(32), nullable=False, default="user")
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    last_login_at = db.Column(db.DateTime(timezone=True))

    @property
    def can_edit_templates(self):
        return self.role in {"template_editor", "admin"}

    @property
    def is_admin(self):
        return self.role == "admin"


class OAuthCredential(TimestampMixin, db.Model):
    __tablename__ = "oauth_credentials"
    id = db.Column(db.String(36), primary_key=True, default=uuid_string)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    encrypted_payload = db.Column(db.LargeBinary, nullable=False)
    key_version = db.Column(db.Integer, nullable=False, default=1)
    scopes = db.Column(db.JSON, nullable=False, default=list)
    refreshed_at = db.Column(db.DateTime(timezone=True))
    user = db.relationship("User", backref=db.backref("oauth_credential", uselist=False, cascade="all, delete-orphan"))


class Campaign(TimestampMixin, db.Model):
    __tablename__ = "campaigns"
    id = db.Column(db.String(36), primary_key=True, default=uuid_string)
    owner_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    sender_email = db.Column(db.String(320), nullable=False)
    source_filename = db.Column(db.String(255))
    upload_path = db.Column(db.String(1000))
    email_column = db.Column(db.String(255))
    name_column = db.Column(db.String(255))
    company_column = db.Column(db.String(255))
    subject = db.Column(db.String(998), nullable=False, default="")
    html_content = db.Column(db.Text, nullable=False, default="")
    text_content = db.Column(db.Text, nullable=False, default="")
    state = db.Column(db.String(32), nullable=False, default="draft", index=True)
    content_hash = db.Column(db.String(64))
    tested_hash = db.Column(db.String(64))
    tested_at = db.Column(db.DateTime(timezone=True))
    queued_at = db.Column(db.DateTime(timezone=True))
    completed_at = db.Column(db.DateTime(timezone=True))
    total_count = db.Column(db.Integer, nullable=False, default=0)
    selected_count = db.Column(db.Integer, nullable=False, default=0)
    sent_count = db.Column(db.Integer, nullable=False, default=0)
    failed_count = db.Column(db.Integer, nullable=False, default=0)
    suppressed_count = db.Column(db.Integer, nullable=False, default=0)
    failure_summary = db.Column(db.String(1000))
    cancellation_requested = db.Column(db.Boolean, nullable=False, default=False)
    owner = db.relationship("User", backref=db.backref("campaigns", lazy="dynamic"))


class CampaignRecipient(TimestampMixin, db.Model):
    __tablename__ = "campaign_recipients"
    id = db.Column(db.String(36), primary_key=True, default=uuid_string)
    campaign_id = db.Column(db.String(36), db.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    source_row = db.Column(db.Integer, nullable=False)
    original_email = db.Column(db.String(1000), nullable=False)
    normalized_email = db.Column(db.String(320), nullable=False, index=True)
    recipient_name = db.Column(db.String(500))
    recipient_company = db.Column(db.String(500))
    validation = db.Column(db.JSON, nullable=False, default=dict)
    selected = db.Column(db.Boolean, nullable=False, default=False)
    suppressed = db.Column(db.Boolean, nullable=False, default=False)
    send_status = db.Column(db.String(32), nullable=False, default="pending")
    gmail_message_id = db.Column(db.String(255))
    error_category = db.Column(db.String(64))
    error_message = db.Column(db.String(1000))
    sent_at = db.Column(db.DateTime(timezone=True))
    campaign = db.relationship("Campaign", backref=db.backref("recipients", cascade="all, delete-orphan", lazy="dynamic"))
    __table_args__ = (db.UniqueConstraint("campaign_id", "normalized_email", name="uq_campaign_recipient"),)


class ValidationJob(TimestampMixin, db.Model):
    __tablename__ = "validation_jobs"
    id = db.Column(db.String(36), primary_key=True, default=uuid_string)
    campaign_id = db.Column(db.String(36), db.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    owner_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    rq_job_id = db.Column(db.String(64), unique=True)
    state = db.Column(db.String(32), nullable=False, default="queued")
    policy = db.Column(db.String(16), nullable=False, default="balanced")
    smtp_enabled = db.Column(db.Boolean, nullable=False, default=False)
    current = db.Column(db.Integer, nullable=False, default=0)
    total = db.Column(db.Integer, nullable=False, default=0)
    valid_count = db.Column(db.Integer, nullable=False, default=0)
    problematic_count = db.Column(db.Integer, nullable=False, default=0)
    message = db.Column(db.String(500))
    error = db.Column(db.String(1000))


class EmailTemplate(TimestampMixin, db.Model):
    __tablename__ = "email_templates"
    id = db.Column(db.String(36), primary_key=True, default=uuid_string)
    name = db.Column(db.String(100), nullable=False, unique=True)
    subject = db.Column(db.String(998), nullable=False)
    html_content = db.Column(db.Text, nullable=False)
    text_content = db.Column(db.Text, nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1)
    created_by_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    updated_by_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    deleted_at = db.Column(db.DateTime(timezone=True))
    creator = db.relationship("User", foreign_keys=[created_by_id])
    updater = db.relationship("User", foreign_keys=[updated_by_id])


class TemplateAsset(TimestampMixin, db.Model):
    __tablename__ = "template_assets"
    id = db.Column(db.String(36), primary_key=True, default=uuid_string)
    template_id = db.Column(db.String(36), db.ForeignKey("email_templates.id", ondelete="SET NULL"), index=True)
    storage_name = db.Column(db.String(255), nullable=False, unique=True)
    original_name = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(64), nullable=False)
    width = db.Column(db.Integer, nullable=False)
    height = db.Column(db.Integer, nullable=False)
    byte_size = db.Column(db.Integer, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    uploaded_by_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    template = db.relationship("EmailTemplate", backref="assets")


class Suppression(TimestampMixin, db.Model):
    __tablename__ = "suppressions"
    id = db.Column(db.String(36), primary_key=True, default=uuid_string)
    normalized_email = db.Column(db.String(320), nullable=False, unique=True, index=True)
    reason = db.Column(db.String(255), nullable=False)
    source = db.Column(db.String(64), nullable=False, default="manual")
    source_campaign_id = db.Column(db.String(36), db.ForeignKey("campaigns.id", ondelete="SET NULL"))
    created_by_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="SET NULL"))
    removed_at = db.Column(db.DateTime(timezone=True))
    removed_by_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="SET NULL"))


class AuditEvent(db.Model):
    __tablename__ = "audit_events"
    id = db.Column(db.String(36), primary_key=True, default=uuid_string)
    actor_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="SET NULL"), index=True)
    action = db.Column(db.String(100), nullable=False, index=True)
    object_type = db.Column(db.String(64), nullable=False)
    object_id = db.Column(db.String(64))
    details = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)


class IdempotencyKey(db.Model):
    __tablename__ = "idempotency_keys"
    id = db.Column(db.String(36), primary_key=True, default=uuid_string)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    action = db.Column(db.String(64), nullable=False)
    key = db.Column(db.String(128), nullable=False)
    result_id = db.Column(db.String(64))
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (db.UniqueConstraint("user_id", "action", "key", name="uq_idempotency"),)


class VerificationCache(db.Model):
    __tablename__ = "verification_cache"
    cache_key = db.Column(db.String(512), primary_key=True)
    kind = db.Column(db.String(16), nullable=False)
    result = db.Column(db.JSON, nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
