"""Durable RQ tasks. Job arguments contain identifiers, never OAuth secrets."""
from __future__ import annotations

import time
import asyncio
from datetime import timedelta

from googleapiclient.errors import HttpError

from email_sender_service import build_message
from extensions import db
from gmail_sender_service import send_one_gmail_message
from models import (AuditEvent, Campaign, CampaignRecipient, IdempotencyKey,
                    TemplateAsset, User, ValidationJob, utcnow)
from production_services import (RedisVerificationCache, enforce_suppressions,
                                 load_encrypted_credentials, save_encrypted_credentials)
from email_validator_service import validate_email_list


def _app():
    from app import app
    return app


def send_campaign_task(campaign_id: str):
    """Send a campaign conservatively, persisting every definitive outcome."""
    with _app().app_context():
        campaign = db.session.get(Campaign, campaign_id)
        if not campaign or campaign.state != "queued":
            return {"status": "ignored"}
        owner = db.session.get(User, campaign.owner_id)
        if not owner or not owner.enabled:
            campaign.state = "failed"
            campaign.failure_summary = "Sender account is disabled."
            db.session.commit()
            return {"status": "failed"}

        enforce_suppressions(campaign)
        credentials = load_encrypted_credentials(owner)
        if not credentials:
            campaign.state = "failed"
            campaign.failure_summary = "Google account must be reconnected."
            db.session.commit()
            return {"status": "failed"}

        campaign.state = "sending"
        db.session.commit()
        recipients = CampaignRecipient.query.filter_by(
            campaign_id=campaign.id, selected=True, suppressed=False, send_status="pending"
        ).order_by(CampaignRecipient.source_row).all()

        for recipient in recipients:
            db.session.refresh(campaign)
            if campaign.cancellation_requested:
                campaign.state = "cancelled"
                db.session.commit()
                return {"status": "cancelled", "sent": campaign.sent_count}
            try:
                message = build_message(
                    recipient.normalized_email,
                    recipient.recipient_name or "",
                    campaign.subject,
                    campaign.html_content,
                    campaign.text_content,
                    "me",
                    inline_image_folder=_app().config["EMAIL_ASSET_FOLDER"],
                    company=recipient.recipient_company or "",
                )
                response = send_one_gmail_message(credentials, message)
                recipient.gmail_message_id = response.get("id")
                recipient.send_status = "sent"
                recipient.sent_at = utcnow()
                campaign.sent_count += 1
                save_encrypted_credentials(owner, credentials)
                db.session.commit()
            except HttpError as exc:
                status = getattr(exc.resp, "status", None)
                # Ambiguous failures are deliberately not retried automatically.
                recipient.send_status = "failed"
                recipient.error_category = "google_transient" if status in {429, 500, 502, 503, 504} else "google_permanent"
                recipient.error_message = f"Gmail API HTTP {status or 'unknown'}"
                campaign.failed_count += 1
                db.session.commit()
                if status in {429, 500, 502, 503, 504}:
                    time.sleep(2)
            except Exception as exc:
                recipient.send_status = "failed"
                recipient.error_category = "build_or_unknown"
                recipient.error_message = str(exc)[:1000]
                campaign.failed_count += 1
                db.session.commit()

        campaign.state = "completed" if campaign.sent_count else "failed"
        campaign.completed_at = utcnow()
        if campaign.state == "failed" and not campaign.failure_summary:
            campaign.failure_summary = "No messages were accepted by Gmail."
        db.session.commit()
        return {"status": campaign.state, "sent": campaign.sent_count, "failed": campaign.failed_count}


def validate_campaign_task(validation_job_id: str):
    """Run comprehensive validation outside the web process and persist results."""
    import pandas as pd

    with _app().app_context():
        job = db.session.get(ValidationJob, validation_job_id)
        if not job or job.state != "queued":
            return {"status": "ignored"}
        campaign = db.session.get(Campaign, job.campaign_id)
        job.state = "running"
        job.message = "Running syntax and domain checks..."
        campaign.state = "validating"
        db.session.commit()

        recipients = CampaignRecipient.query.filter_by(campaign_id=campaign.id).order_by(
            CampaignRecipient.source_row
        ).all()
        frame = pd.DataFrame({"email": [item.normalized_email for item in recipients]})
        try:
            validated = asyncio.run(validate_email_list(
                frame,
                "email",
                do_smtp=job.smtp_enabled,
                mail_from=_app().config.get("VALIDATION_MAIL_FROM", ""),
                policy=job.policy,
                cache=RedisVerificationCache(_app().config["REDIS_URL"]),
            ))
            valid_count = 0
            for recipient, (_, result) in zip(recipients, validated.iterrows()):
                payload = {}
                for key, value in result.to_dict().items():
                    if pd.isna(value):
                        payload[key] = None
                    elif hasattr(value, "item"):
                        payload[key] = value.item()
                    else:
                        payload[key] = value
                recipient.validation = payload
                recipient.selected = not bool(payload.get("bounce_risk"))
                if recipient.selected:
                    valid_count += 1
            enforce_suppressions(campaign)
            campaign.state = "reviewed"
            campaign.selected_count = sum(1 for item in recipients if item.selected and not item.suppressed)
            job.state = "completed"
            job.current = len(recipients)
            job.valid_count = valid_count
            job.problematic_count = len(recipients) - valid_count
            job.message = "Validation complete."
            db.session.commit()
            return {"status": "completed", "valid": valid_count}
        except Exception as exc:
            campaign.state = "draft"
            job.state = "failed"
            job.error = str(exc)[:1000]
            job.message = "Validation failed."
            db.session.commit()
            raise


def retention_cleanup_task():
    """Remove expired personal data while retaining aggregate campaign history."""
    with _app().app_context():
        cutoff = utcnow() - timedelta(days=_app().config["RETENTION_DAYS"])
        expired = Campaign.query.filter(Campaign.created_at < cutoff).all()
        recipient_count = 0
        upload_count = 0
        for campaign in expired:
            recipient_count += campaign.recipients.delete(synchronize_session=False)
            if campaign.upload_path:
                from pathlib import Path
                path = Path(campaign.upload_path)
                if path.is_file():
                    path.unlink()
                    upload_count += 1
                campaign.upload_path = None
        audit_cutoff = utcnow() - timedelta(days=_app().config["AUDIT_RETENTION_DAYS"])
        audit_count = AuditEvent.query.filter(AuditEvent.created_at < audit_cutoff).delete(synchronize_session=False)
        key_count = IdempotencyKey.query.filter(IdempotencyKey.expires_at < utcnow()).delete(synchronize_session=False)
        orphan_assets = TemplateAsset.query.filter(TemplateAsset.template_id.is_(None),
                                                    TemplateAsset.created_at < cutoff).all()
        asset_count = 0
        from pathlib import Path
        for asset in orphan_assets:
            path = Path(_app().config["EMAIL_ASSET_FOLDER"]) / asset.storage_name
            if path.is_file():
                path.unlink()
            db.session.delete(asset)
            asset_count += 1
        db.session.commit()
        return {"recipients_deleted": recipient_count, "uploads_deleted": upload_count,
                "audit_deleted": audit_count, "idempotency_deleted": key_count,
                "orphan_assets_deleted": asset_count}
