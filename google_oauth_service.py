"""
Google OAuth helpers for Gmail API send
"""
import json
import os
from pathlib import Path
from typing import Optional, Tuple

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/userinfo.email"
]
CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:5000/oauth2/callback")
TOKEN_STORE = Path(os.getenv("GOOGLE_TOKEN_STORE", "tokens.json"))


def _client_config() -> dict:
    return {
        "web": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uris": [REDIRECT_URI],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def create_flow() -> Flow:
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("Google OAuth not configured. Set GOOGLE_CLIENT_ID/SECRET in .env")
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES)
    flow.redirect_uri = REDIRECT_URI
    return flow


def generate_auth_url() -> Tuple[str, str]:
    flow = create_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return auth_url, state


def exchange_code(code: str) -> Credentials:
    flow = create_flow()
    flow.fetch_token(code=code)
    return flow.credentials


def save_credentials(email: str, creds: Credentials):
    TOKEN_STORE.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if TOKEN_STORE.exists():
        try:
            data = json.loads(TOKEN_STORE.read_text())
        except Exception:
            data = {}
    data[email] = creds.to_json()
    TOKEN_STORE.write_text(json.dumps(data, indent=2))


def load_credentials(email: str) -> Optional[Credentials]:
    if not TOKEN_STORE.exists():
        return None
    try:
        data = json.loads(TOKEN_STORE.read_text())
    except Exception:
        return None
    raw = data.get(email)
    if not raw:
        return None
    return Credentials.from_authorized_user_info(json.loads(raw), scopes=SCOPES)


def ensure_valid_credentials(creds: Credentials) -> Credentials:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def get_profile_email(creds: Credentials) -> str:
    """Get user's email address from OAuth2 userinfo"""
    from googleapiclient.discovery import build
    service = build("oauth2", "v2", credentials=creds, cache_discovery=False)
    user_info = service.userinfo().get().execute()
    return user_info.get("email")
