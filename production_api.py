"""Authenticated resource APIs, administration, and operational endpoints."""
from __future__ import annotations

import csv
import io
import os
import uuid
from datetime import timedelta
from pathlib import Path

import pandas as pd
from flask import Blueprint, Response, current_app, g, jsonify, redirect, render_template, request, session, url_for
from googleapiclient.errors import HttpError
from redis import Redis
from rq import Queue
from sqlalchemy import select

from email_sender_service import build_message
from extensions import db, limiter
from gmail_sender_service import send_one_gmail_message
from models import (
    AuditEvent, Campaign, CampaignRecipient, EmailTemplate, IdempotencyKey,
    OAuthCredential, Suppression, User, ValidationJob, utcnow,
)
from production_services import (
    audit, campaign_hash, current_user, enforce_suppressions, load_encrypted_credentials,
    login_required, normalize_email, owned_campaign, roles_required, sanitize_email_html,
)

production = Blueprint("production", __name__)


def _user_rate_key():
    """Keep one authenticated office user's traffic out of another's budget."""
    user = current_user()
    return f"user:{user.id}" if user else f"ip:{request.remote_addr or 'unknown'}"


def _queue(name="default"):
    return Queue(name, connection=Redis.from_url(current_app.config["REDIS_URL"]))


def _campaign_payload(campaign):
    return {
        "id": campaign.id,
        "state": campaign.state,
        "sender_email": campaign.sender_email,
        "subject": campaign.subject,
        "total": campaign.total_count,
        "selected": campaign.selected_count,
        "suppressed": campaign.suppressed_count,
        "sent": campaign.sent_count,
        "failed": campaign.failed_count,
        "tested": bool(campaign.tested_hash and campaign.tested_hash == campaign_hash(campaign)),
        "created_at": campaign.created_at.isoformat(),
        "updated_at": campaign.updated_at.isoformat(),
    }


def _csv_safe(value):
    text = str(value or "")
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


@production.get("/login")
def login_page():
    if current_user():
        return redirect(url_for("index"))
    return render_template("login.html", allowed_domain=current_app.config["ALLOWED_GOOGLE_DOMAIN"])


@production.get("/health/live")
def health_live():
    return jsonify({"status": "ok"})


@production.get("/health/ready")
def health_ready():
    try:
        db.session.execute(db.text("SELECT 1"))
        Redis.from_url(current_app.config["REDIS_URL"], socket_timeout=1).ping()
    except Exception:
        return jsonify({"status": "unavailable"}), 503
    return jsonify({"status": "ready"})


@production.get("/api/me")
@login_required
def api_me():
    user = current_user()
    return jsonify({"id": user.id, "email": user.email, "name": user.display_name,
                    "role": user.role, "google_connected": bool(user.oauth_credential)})


@production.post("/auth/logout")
@login_required
def auth_logout():
    session.clear()
    return redirect(url_for("production.login_page"))


@production.delete("/api/me/google-credentials")
@login_required
def disconnect_google():
    credential = current_user().oauth_credential
    if credential:
        db.session.delete(credential)
        audit("oauth.disconnected", "user", current_user().id)
        db.session.commit()
    session.clear()
    return jsonify({"success": True})


@production.get("/api/campaigns")
@login_required
def list_campaigns():
    query = select(Campaign).order_by(Campaign.created_at.desc())
    if not current_user().is_admin:
        query = query.where(Campaign.owner_id == current_user().id)
    campaigns = db.session.scalars(query.limit(100)).all()
    return jsonify({"campaigns": [_campaign_payload(item) for item in campaigns]})


@production.post("/api/campaigns")
@login_required
@limiter.limit("60 per hour", key_func=_user_rate_key)
def create_campaign():
    campaign = Campaign(owner_id=current_user().id, sender_email=current_user().email)
    db.session.add(campaign)
    audit("campaign.created", "campaign", campaign.id)
    db.session.commit()
    return jsonify({"campaign": _campaign_payload(campaign)}), 201


@production.get("/api/campaigns/<campaign_id>")
@login_required
def get_campaign(campaign_id):
    campaign = owned_campaign(campaign_id)
    payload = _campaign_payload(campaign)
    payload.update({"html_content": campaign.html_content, "text_content": campaign.text_content})
    return jsonify({"campaign": payload})


@production.post("/api/campaigns/<campaign_id>/upload")
@login_required
@limiter.limit("60 per hour", key_func=_user_rate_key)
def upload_campaign_csv(campaign_id):
    campaign = owned_campaign(campaign_id, admin_ok=False)
    if campaign.state not in {"draft", "reviewed"}:
        return jsonify({"error": "This campaign can no longer accept uploads."}), 409
    uploaded = request.files.get("csv_file")
    if not uploaded or not uploaded.filename or not uploaded.filename.lower().endswith(".csv"):
        return jsonify({"error": "Choose a CSV file."}), 400

    raw = uploaded.read(current_app.config["MAX_CONTENT_LENGTH"] + 1)
    if len(raw) > current_app.config["MAX_CONTENT_LENGTH"]:
        return jsonify({"error": "CSV exceeds the 16 MB limit."}), 413
    try:
        text = raw.decode("utf-8-sig")
        frame = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    except (UnicodeDecodeError, pd.errors.ParserError) as exc:
        return jsonify({"error": "CSV must be valid UTF-8 with a readable header."}), 400
    if frame.empty or len(frame) > current_app.config["MAX_CSV_ROWS"]:
        return jsonify({"error": f"CSV must contain 1-{current_app.config['MAX_CSV_ROWS']} rows."}), 400
    if len(frame.columns) > current_app.config["MAX_CSV_COLUMNS"] or frame.columns.duplicated().any():
        return jsonify({"error": "CSV has too many or duplicate columns."}), 400
    if any(frame[column].astype(str).str.len().max() > current_app.config["MAX_FIELD_LENGTH"] for column in frame.columns):
        return jsonify({"error": "CSV contains an excessively long field."}), 400

    requested_email_col = request.form.get("email_column", "")
    email_column = requested_email_col if requested_email_col in frame.columns else next(
        (column for column in frame.columns if "mail" in column.lower()), None
    )
    name_column = request.form.get("name_column") or next(
        (column for column in frame.columns if "name" in column.lower()), None
    )
    company_column = request.form.get("company_column") or next(
        (column for column in frame.columns
         if any(term in column.lower() for term in ("company", "organisation", "organization", "institution"))),
        None,
    )
    if not email_column:
        return jsonify({"error": "No email column could be identified."}), 400

    storage = Path(current_app.config["UPLOAD_FOLDER"])
    storage.mkdir(parents=True, exist_ok=True)
    upload_path = storage / f"{campaign.id}-{uuid.uuid4().hex}.csv"
    upload_path.write_bytes(raw)

    campaign.recipients.delete(synchronize_session=False)
    seen = set()
    for source_row, row in frame.iterrows():
        original = str(row[email_column]).strip()
        normalized = normalize_email(original)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        db.session.add(CampaignRecipient(
            campaign_id=campaign.id,
            source_row=int(source_row) + 2,
            original_email=original,
            normalized_email=normalized,
            recipient_name=str(row[name_column]).strip() if name_column else "",
            recipient_company=str(row[company_column]).strip() if company_column else "",
        ))
    campaign.source_filename = Path(uploaded.filename).name[:255]
    campaign.upload_path = str(upload_path)
    campaign.email_column = email_column
    campaign.name_column = name_column
    campaign.company_column = company_column
    campaign.total_count = len(seen)
    campaign.state = "draft"
    campaign.tested_hash = None
    audit("campaign.uploaded", "campaign", campaign.id, {"rows": len(seen)})
    db.session.commit()
    return jsonify({"campaign": _campaign_payload(campaign), "columns": list(frame.columns),
                    "company_column": company_column})


@production.post("/api/campaigns/<campaign_id>/validate")
@login_required
@limiter.limit("30 per hour", key_func=_user_rate_key)
def validate_campaign(campaign_id):
    campaign = owned_campaign(campaign_id, admin_ok=False)
    if not campaign.total_count:
        return jsonify({"error": "Upload recipients first."}), 409
    active_job = db.session.scalar(
        select(ValidationJob)
        .where(ValidationJob.campaign_id == campaign.id,
               ValidationJob.state.in_(("queued", "running")))
        .order_by(ValidationJob.created_at.desc())
    )
    if active_job:
        return jsonify({"job_id": active_job.id, "state": active_job.state,
                        "existing": True}), 202
    data = request.get_json(silent=True) or {}
    policy = str(data.get("policy", "balanced")).lower()
    smtp_enabled = bool(data.get("smtp_enabled", False))
    if policy not in {"strict", "balanced", "relaxed"}:
        return jsonify({"error": "Invalid validation policy."}), 400
    if smtp_enabled and not current_app.config.get("VALIDATION_MAIL_FROM"):
        return jsonify({"error": "SMTP validation sender is not configured."}), 400
    job = ValidationJob(campaign_id=campaign.id, owner_id=current_user().id,
                        policy=policy, smtp_enabled=smtp_enabled, total=campaign.total_count)
    db.session.add(job)
    db.session.flush()
    rq_job = _queue("validation").enqueue("tasks.validate_campaign_task", job.id,
                                           job_timeout="30m", result_ttl=86400)
    job.rq_job_id = rq_job.id
    campaign.state = "validating"
    audit("campaign.validation_queued", "campaign", campaign.id, {"job_id": job.id})
    db.session.commit()
    return jsonify({"job_id": job.id, "state": job.state}), 202


@production.get("/api/jobs/<job_id>")
@login_required
@limiter.limit("90 per minute; 3000 per hour", key_func=_user_rate_key)
def job_status(job_id):
    job = db.session.get(ValidationJob, job_id)
    if job:
        if job.owner_id != current_user().id and not current_user().is_admin:
            return jsonify({"error": "Job not found."}), 404
        return jsonify({"id": job.id, "type": "validation", "state": job.state,
                        "current": job.current, "total": job.total, "message": job.message,
                        "error": job.error, "valid": job.valid_count,
                        "problematic": job.problematic_count})
    campaign = db.session.get(Campaign, job_id)
    if campaign and (campaign.owner_id == current_user().id or current_user().is_admin):
        return jsonify({"id": campaign.id, "type": "campaign", **_campaign_payload(campaign)})
    return jsonify({"error": "Job not found."}), 404


@production.get("/api/campaigns/<campaign_id>/recipients")
@login_required
def get_recipients(campaign_id):
    campaign = owned_campaign(campaign_id)
    recipients = campaign.recipients.order_by(CampaignRecipient.source_row).limit(10000).all()
    return jsonify({"recipients": [{"id": item.id, "email": item.normalized_email,
                                    "name": item.recipient_name, "company": item.recipient_company,
                                    "selected": item.selected,
                                    "suppressed": item.suppressed, "validation": item.validation,
                                    "send_status": item.send_status} for item in recipients]})


@production.patch("/api/campaigns/<campaign_id>/recipients")
@login_required
def update_recipients(campaign_id):
    campaign = owned_campaign(campaign_id, admin_ok=False)
    if campaign.state not in {"reviewed", "composed", "test_required", "ready"}:
        return jsonify({"error": "Recipients cannot be changed in this campaign state."}), 409
    selected_ids = set((request.get_json(silent=True) or {}).get("selected_ids", []))
    for item in campaign.recipients:
        item.selected = item.id in selected_ids and not item.suppressed
    enforce_suppressions(campaign)
    if campaign.selected_count > current_app.config["MAX_CAMPAIGN_RECIPIENTS"]:
        db.session.rollback()
        return jsonify({"error": f"Campaigns are limited to {current_app.config['MAX_CAMPAIGN_RECIPIENTS']} recipients."}), 400
    campaign.tested_hash = None
    campaign.state = "test_required" if campaign.subject else "reviewed"
    db.session.commit()
    return jsonify({"campaign": _campaign_payload(campaign)})


@production.put("/api/campaigns/<campaign_id>/content")
@login_required
def update_campaign_content(campaign_id):
    campaign = owned_campaign(campaign_id, admin_ok=False)
    if campaign.state in {"queued", "sending", "completed", "cancelled"}:
        return jsonify({"error": "Campaign content is locked."}), 409
    data = request.get_json(silent=True) or {}
    subject = str(data.get("subject") or "").strip()
    html = sanitize_email_html(str(data.get("html_content") or ""))
    text = str(data.get("text_content") or "").strip()
    if not subject or not html or not text:
        return jsonify({"error": "Subject, HTML and plain text are required."}), 400
    campaign.subject, campaign.html_content, campaign.text_content = subject, html, text
    campaign.state = "test_required"
    campaign.tested_hash = None
    campaign.content_hash = campaign_hash(campaign)
    audit("campaign.content_updated", "campaign", campaign.id)
    db.session.commit()
    payload = _campaign_payload(campaign)
    payload.update({"html_content": campaign.html_content, "text_content": campaign.text_content})
    return jsonify({"campaign": payload})


@production.post("/api/campaigns/<campaign_id>/test")
@login_required
@limiter.limit("30 per hour", key_func=_user_rate_key)
def test_campaign(campaign_id):
    campaign = owned_campaign(campaign_id, admin_ok=False)
    if not campaign.subject or not campaign.selected_count:
        return jsonify({"error": "Compose the campaign and select recipients first."}), 409
    if current_app.config["APP_ENV"] == "staging" and current_user().email not in current_app.config["STAGING_SEND_ALLOWLIST"]:
        return jsonify({"error": "Your address is not in the staging test allowlist."}), 403
    credentials = load_encrypted_credentials(current_user())
    if not credentials:
        return jsonify({"error": "Reconnect your Google account."}), 401
    first = campaign.recipients.filter_by(selected=True, suppressed=False).first()
    message = build_message(current_user().email, first.recipient_name or "", campaign.subject,
                            campaign.html_content, campaign.text_content, "me",
                            inline_image_folder=current_app.config["EMAIL_ASSET_FOLDER"],
                            company=first.recipient_company or "")
    try:
        response = send_one_gmail_message(credentials, message)
    except HttpError:
        return jsonify({"error": "Gmail rejected the test message. Reconnect or try again."}), 502
    campaign.content_hash = campaign_hash(campaign)
    campaign.tested_hash = campaign.content_hash
    campaign.tested_at = utcnow()
    campaign.state = "ready"
    from production_services import save_encrypted_credentials
    save_encrypted_credentials(current_user(), credentials)
    audit("campaign.test_sent", "campaign", campaign.id, {"gmail_message_id": response.get("id")})
    db.session.commit()
    return jsonify({"success": True, "campaign": _campaign_payload(campaign)})


@production.post("/api/campaigns/<campaign_id>/queue")
@login_required
@limiter.limit("30 per hour", key_func=_user_rate_key)
def queue_campaign(campaign_id):
    campaign = owned_campaign(campaign_id, admin_ok=False)
    data = request.get_json(silent=True) or {}
    if current_app.config["APP_ENV"] == "staging":
        return jsonify({"error": "Staging permits test messages only; live campaign queueing is disabled."}), 403
    # Serialize queue decisions for one sending identity.
    db.session.execute(select(User).where(User.id == current_user().id).with_for_update())
    enforce_suppressions(campaign)
    expected = f"SEND {campaign.selected_count}"
    if data.get("confirmation") != expected:
        return jsonify({"error": f"Type {expected} to confirm."}), 400
    if not campaign.selected_count or campaign.selected_count > current_app.config["MAX_CAMPAIGN_RECIPIENTS"]:
        return jsonify({"error": "Recipient count is outside the allowed range."}), 400
    current_hash = campaign_hash(campaign)
    if campaign.state != "ready" or campaign.tested_hash != current_hash:
        return jsonify({"error": "Send a new test after the latest changes."}), 409
    active = Campaign.query.filter(Campaign.sender_email == current_user().email,
                                   Campaign.state.in_(["queued", "sending"]), Campaign.id != campaign.id).first()
    if active:
        return jsonify({"error": "This sender already has an active campaign."}), 409
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key or len(key) > 128:
        return jsonify({"error": "A valid Idempotency-Key header is required."}), 400
    existing = IdempotencyKey.query.filter_by(user_id=current_user().id, action="queue_campaign", key=key).first()
    if existing:
        return jsonify({"campaign_id": existing.result_id, "duplicate": True}), 200
    db.session.add(IdempotencyKey(user_id=current_user().id, action="queue_campaign", key=key,
                                  result_id=campaign.id, expires_at=utcnow() + timedelta(days=1)))
    campaign.state = "queued"
    campaign.queued_at = utcnow()
    audit("campaign.queued", "campaign", campaign.id, {"recipients": campaign.selected_count})
    db.session.commit()
    try:
        _queue("campaigns").enqueue("tasks.send_campaign_task", campaign.id,
                                     job_id=f"campaign-{campaign.id}", job_timeout="24h", result_ttl=86400)
    except Exception:
        campaign.state = "ready"
        campaign.queued_at = None
        IdempotencyKey.query.filter_by(user_id=current_user().id, action="queue_campaign", key=key).delete()
        db.session.commit()
        current_app.logger.exception("Campaign enqueue failed")
        return jsonify({"error": "The campaign queue is temporarily unavailable."}), 503
    return jsonify({"campaign_id": campaign.id, "state": campaign.state}), 202


@production.post("/api/campaigns/<campaign_id>/cancel")
@login_required
def cancel_campaign(campaign_id):
    campaign = owned_campaign(campaign_id, admin_ok=False)
    if campaign.state not in {"queued", "sending"}:
        return jsonify({"error": "Only queued or sending campaigns can be cancelled."}), 409
    campaign.cancellation_requested = True
    audit("campaign.cancel_requested", "campaign", campaign.id)
    db.session.commit()
    return jsonify({"success": True})


@production.get("/api/campaigns/<campaign_id>/results")
@login_required
def campaign_results(campaign_id):
    campaign = owned_campaign(campaign_id)
    rows = campaign.recipients.order_by(CampaignRecipient.source_row).all()
    if request.args.get("format") == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["email", "name", "company", "status", "gmail_message_id", "error_category"])
        for item in rows:
            writer.writerow([_csv_safe(item.normalized_email), _csv_safe(item.recipient_name),
                             _csv_safe(item.recipient_company),
                             _csv_safe(item.send_status), _csv_safe(item.gmail_message_id),
                             _csv_safe(item.error_category)])
        return Response(output.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=campaign-{campaign.id}-results.csv"})
    return jsonify({"campaign": _campaign_payload(campaign), "recipients": [
        {"email": item.normalized_email, "name": item.recipient_name,
         "company": item.recipient_company, "status": item.send_status,
         "gmail_message_id": item.gmail_message_id, "error_category": item.error_category}
        for item in rows
    ]})


@production.get("/api/admin/users")
@roles_required("admin")
def admin_users():
    users = User.query.order_by(User.email).all()
    return jsonify({"users": [{"id": item.id, "email": item.email, "name": item.display_name,
                               "role": item.role, "enabled": item.enabled} for item in users]})


@production.get("/admin")
@roles_required("admin")
def admin_page():
    return render_template("admin.html")


@production.patch("/api/admin/users/<user_id>")
@roles_required("admin")
def admin_update_user(user_id):
    target = db.session.get(User, user_id)
    if not target:
        return jsonify({"error": "User not found."}), 404
    data = request.get_json(silent=True) or {}
    new_role = data.get("role", target.role)
    enabled = bool(data.get("enabled", target.enabled))
    if new_role not in {"user", "template_editor", "admin"}:
        return jsonify({"error": "Invalid role."}), 400
    enabled_admins = User.query.filter_by(role="admin", enabled=True).count()
    removing_last = target.role == "admin" and target.enabled and (new_role != "admin" or not enabled) and enabled_admins <= 1
    if removing_last:
        return jsonify({"error": "The final enabled administrator cannot be removed."}), 409
    target.role, target.enabled = new_role, enabled
    if not enabled and target.oauth_credential:
        db.session.delete(target.oauth_credential)
    audit("user.updated", "user", target.id, {"role": new_role, "enabled": enabled})
    db.session.commit()
    return jsonify({"success": True})


@production.get("/api/admin/suppressions")
@roles_required("admin")
def list_suppressions():
    rows = Suppression.query.filter_by(removed_at=None).order_by(Suppression.created_at.desc()).all()
    return jsonify({"suppressions": [{"id": item.id, "email": item.normalized_email,
                                      "reason": item.reason, "source": item.source} for item in rows]})


@production.post("/api/admin/suppressions")
@roles_required("admin")
def add_suppression():
    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get("email"))
    reason = str(data.get("reason") or "Manual suppression").strip()[:255]
    if "@" not in email:
        return jsonify({"error": "Enter a valid email address."}), 400
    row = Suppression.query.filter_by(normalized_email=email).first()
    if row:
        row.removed_at = None
        row.removed_by_id = None
        row.reason = reason
        row.source = "manual"
    else:
        row = Suppression(normalized_email=email, reason=reason, source="manual",
                          created_by_id=current_user().id)
        db.session.add(row)
        audit("suppression.created", "suppression", row.id, {"email_domain": email.rsplit("@", 1)[-1]})
        db.session.commit()
    return jsonify({"id": row.id, "email": row.normalized_email}), 201


@production.delete("/api/admin/suppressions/<suppression_id>")
@roles_required("admin")
def delete_suppression(suppression_id):
    row = db.session.get(Suppression, suppression_id)
    if not row:
        return jsonify({"error": "Suppression not found."}), 404
    row.removed_at = utcnow()
    row.removed_by_id = current_user().id
    audit("suppression.deleted", "suppression", suppression_id)
    db.session.commit()
    return jsonify({"success": True})


@production.get("/api/admin/suppressions/export")
@roles_required("admin")
def export_suppressions():
    rows = Suppression.query.filter_by(removed_at=None).order_by(Suppression.normalized_email).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["email", "reason", "source", "created_at"])
    for item in rows:
        writer.writerow([_csv_safe(item.normalized_email), _csv_safe(item.reason),
                         _csv_safe(item.source), item.created_at.isoformat()])
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=gc-emailer-suppressions.csv"})


@production.post("/api/admin/suppressions/import")
@roles_required("admin")
def import_suppressions():
    uploaded = request.files.get("csv_file")
    if not uploaded:
        return jsonify({"error": "Choose a CSV file."}), 400
    try:
        frame = pd.read_csv(uploaded, dtype=str, keep_default_na=False)
    except pd.errors.ParserError:
        return jsonify({"error": "Suppression CSV is invalid."}), 400
    email_column = next((column for column in frame.columns if column.lower() == "email"), None)
    if not email_column or len(frame) > 10000:
        return jsonify({"error": "CSV needs an email column and at most 10,000 rows."}), 400
    imported = 0
    for value in frame[email_column]:
        email = normalize_email(value)
        if "@" not in email:
            continue
        row = Suppression.query.filter_by(normalized_email=email).first()
        if not row:
            row = Suppression(normalized_email=email, reason="Imported suppression", source="import",
                              created_by_id=current_user().id)
            db.session.add(row)
        else:
            row.removed_at = None
            row.removed_by_id = None
        imported += 1
    audit("suppression.imported", "suppression", details={"count": imported})
    db.session.commit()
    return jsonify({"success": True, "imported": imported})


@production.get("/api/admin/jobs")
@roles_required("admin")
def admin_jobs():
    rows = Campaign.query.filter(Campaign.state.in_(["queued", "sending", "failed"])) \
        .order_by(Campaign.updated_at.desc()).limit(200).all()
    return jsonify({"jobs": [_campaign_payload(item) for item in rows]})


@production.post("/api/admin/retention")
@roles_required("admin")
def run_retention():
    job = _queue("maintenance").enqueue("tasks.retention_cleanup_task", job_timeout="30m")
    audit("retention.queued", "maintenance", job.id)
    db.session.commit()
    return jsonify({"success": True, "job_id": job.id}), 202


@production.get("/api/admin/audit")
@roles_required("admin")
def audit_log():
    rows = AuditEvent.query.order_by(AuditEvent.created_at.desc()).limit(500).all()
    return jsonify({"events": [{"id": item.id, "actor_id": item.actor_id, "action": item.action,
                                "object_type": item.object_type, "object_id": item.object_id,
                                "details": item.details, "created_at": item.created_at.isoformat()}
                               for item in rows]})
