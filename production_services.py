"""Security, persistence, and campaign services used by production routes/workers."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import timedelta
from functools import wraps

import bleach
from bleach.css_sanitizer import CSSSanitizer
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from flask import abort, current_app, g, jsonify, redirect, request, session, url_for
from sqlalchemy import select
from redis import Redis

from extensions import db
from models import AuditEvent, Campaign, CampaignRecipient, OAuthCredential, Suppression, User, utcnow


ALLOWED_TEMPLATE_TAGS = {
    "a", "b", "blockquote", "br", "center", "div", "em", "font", "h1", "h2", "h3",
    "h4", "hr", "i", "img", "li", "ol", "p", "span", "strong", "table", "tbody",
    "td", "tfoot", "th", "thead", "tr", "u", "ul",
}
ALLOWED_TEMPLATE_ATTRIBUTES = {
    "*": ["class", "style", "align", "valign", "width", "height", "role"],
    "a": ["href", "target", "title"],
    "img": ["src", "alt", "width", "height", "style", "border"],
    "table": ["cellpadding", "cellspacing", "border", "bgcolor"],
    "td": ["colspan", "rowspan", "bgcolor"],
}


def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def sanitize_email_html(value: str) -> str:
    """Keep email-compatible markup while removing executable browser content."""
    return bleach.clean(
        value or "",
        tags=ALLOWED_TEMPLATE_TAGS,
        attributes=ALLOWED_TEMPLATE_ATTRIBUTES,
        protocols={"http", "https", "mailto", "tel", "cid"},
        css_sanitizer=CSSSanitizer(
            allowed_css_properties={
                "background", "background-color", "border", "border-radius", "color", "display",
                "font-family", "font-size", "font-style", "font-weight", "height", "letter-spacing",
                "line-height", "margin", "margin-bottom", "margin-left", "margin-right", "margin-top",
                "max-width", "min-width", "opacity", "overflow", "padding", "padding-bottom",
                "padding-left", "padding-right", "padding-top", "text-align", "text-decoration",
                "vertical-align", "white-space", "width",
            }
        ),
        strip=True,
        strip_comments=True,
    )


def campaign_hash(campaign: Campaign) -> str:
    selected = db.session.scalars(
        select(CampaignRecipient.normalized_email)
        .where(CampaignRecipient.campaign_id == campaign.id, CampaignRecipient.selected.is_(True))
        .order_by(CampaignRecipient.normalized_email)
    ).all()
    material = json.dumps({
        "subject": campaign.subject,
        "html": campaign.html_content,
        "text": campaign.text_content,
        "recipients": selected,
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def audit(action: str, object_type: str, object_id: str | None = None, details: dict | None = None):
    db.session.add(AuditEvent(
        actor_id=getattr(g.get("current_user"), "id", None),
        action=action,
        object_type=object_type,
        object_id=object_id,
        details=details or {},
    ))


def _fernet() -> MultiFernet:
    raw = current_app.config.get("TOKEN_ENCRYPTION_KEY", "")
    keys = [item.strip().encode() for item in raw.split(",") if item.strip()]
    if not keys:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not configured")
    return MultiFernet([Fernet(key) for key in keys])


def save_encrypted_credentials(user: User, credentials):
    payload = credentials.to_json().encode("utf-8")
    record = user.oauth_credential or OAuthCredential(user=user, encrypted_payload=b"")
    record.encrypted_payload = _fernet().encrypt(payload)
    record.scopes = list(credentials.scopes or [])
    record.refreshed_at = utcnow()
    db.session.add(record)


def load_encrypted_credentials(user: User):
    from google.oauth2.credentials import Credentials

    record = user.oauth_credential
    if not record:
        return None
    try:
        raw = _fernet().decrypt(record.encrypted_payload)
    except InvalidToken as exc:
        raise RuntimeError("Stored Google credentials cannot be decrypted") from exc
    return Credentials.from_authorized_user_info(json.loads(raw), scopes=record.scopes)


def current_user() -> User | None:
    return g.get("current_user")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            if request.path.startswith("/api/"):
                return jsonify({"success": False, "error": "Authentication required."}), 401
            return redirect(url_for("production.login_page", next=request.full_path))
        return view(*args, **kwargs)
    return wrapped


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user().role not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def owned_campaign(campaign_id: str, admin_ok: bool = True) -> Campaign:
    campaign = db.session.get(Campaign, campaign_id)
    user = current_user()
    if not campaign or not user or (campaign.owner_id != user.id and not (admin_ok and user.is_admin)):
        abort(404)
    return campaign


def enforce_suppressions(campaign: Campaign) -> int:
    suppressed_addresses = set(db.session.scalars(
        select(Suppression.normalized_email).where(Suppression.removed_at.is_(None))
    ).all())
    changed = 0
    for recipient in campaign.recipients:
        should_suppress = recipient.normalized_email in suppressed_addresses
        if should_suppress and not recipient.suppressed:
            changed += 1
        recipient.suppressed = should_suppress
        if should_suppress:
            recipient.selected = False
    campaign.suppressed_count = sum(1 for item in campaign.recipients if item.suppressed)
    campaign.selected_count = sum(1 for item in campaign.recipients if item.selected and not item.suppressed)
    return changed


def request_id() -> str:
    value = request.headers.get("X-Request-ID", "")
    return value if re.fullmatch(r"[A-Za-z0-9._-]{1,100}", value) else os.urandom(8).hex()


class RedisVerificationCache:
    """Thread-safe shared verification cache for web and worker processes."""
    def __init__(self, redis_url: str):
        self.client = Redis.from_url(redis_url, decode_responses=True)
        self.valid_ttl = 30 * 86400
        self.soft_ttl = 86400
        self.mx_ttl = 7 * 86400

    @staticmethod
    def _email_key(email):
        return f"gcemailer:verify:email:{normalize_email(email)}"

    @staticmethod
    def _mx_key(domain):
        return f"gcemailer:verify:mx:{str(domain).lower()}"

    def get_email(self, email, force=False):
        if force:
            return None
        raw = self.client.get(self._email_key(email))
        return json.loads(raw) if raw else None

    def put_email(self, result):
        status = result.get("smtp_status")
        ttl = self.valid_ttl if status in {"valid", "invalid"} else self.soft_ttl
        self.client.setex(self._email_key(result.get("normalized")), ttl,
                          json.dumps(result, default=str))

    def get_mx_details(self, domain, force=False):
        if force:
            return None
        raw = self.client.get(self._mx_key(domain))
        return json.loads(raw) if raw else None

    def get_mx(self, domain, force=False):
        result = self.get_mx_details(domain, force)
        return (result.get("mx_ok"), result.get("mx_host"), result.get("error")) if result else None

    def put_mx(self, domain, mx_ok, mx_host, err, status="unknown", attempts=0):
        result = {"mx_ok": bool(mx_ok), "mx_host": mx_host, "error": err,
                  "status": status, "attempts": attempts}
        self.client.setex(self._mx_key(domain), self.mx_ttl, json.dumps(result))

    def close(self):
        self.client.close()
