"""
Google OAuth helpers for Gmail API send
"""
import json
import os
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

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


def _configure_oauth_transport() -> None:
    """Permit HTTP only for a loopback callback in local development."""
    parsed = urlparse(REDIRECT_URI)
    if parsed.scheme == "https":
        return
    environment = os.getenv("APP_ENV", "development").lower()
    if (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            and environment in {"development", "testing"}):
        # oauthlib requires this explicit opt-in even though loopback HTTP is
        # the standard OAuth pattern for a locally running application.
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
        return
    raise RuntimeError("Google OAuth callbacks must use HTTPS outside local development.")


def _client_config() -> dict:
    client_id = os.environ["GOOGLE_CLIENT_ID"] if "GOOGLE_CLIENT_ID" in os.environ else CLIENT_ID
    client_secret = os.environ["GOOGLE_CLIENT_SECRET"] if "GOOGLE_CLIENT_SECRET" in os.environ else CLIENT_SECRET
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": [REDIRECT_URI],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def create_flow(state: Optional[str] = None) -> Flow:
    _configure_oauth_transport()
    config = _client_config()["web"]
    if not config["client_id"] or not config["client_secret"]:
        raise RuntimeError("Google OAuth not configured. Set GOOGLE_CLIENT_ID/SECRET in .env")
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, state=state)
    flow.redirect_uri = REDIRECT_URI
    return flow


def generate_auth_url() -> Tuple[str, str]:
    flow = create_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="false",
        prompt="consent",
    )
    return auth_url, state


def exchange_code(code: str, state: Optional[str] = None,
                  authorization_response: Optional[str] = None) -> Credentials:
    flow = create_flow(state=state)
    if authorization_response:
        flow.fetch_token(authorization_response=authorization_response)
    else:
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


def get_profile(creds: Credentials) -> dict:
    """Return the verified Google identity used for office authorization."""
    service = build("oauth2", "v2", credentials=creds, cache_discovery=False)
    user_info = service.userinfo().get().execute()
    return {
        "email": str(user_info.get("email") or "").strip().lower(),
        "verified_email": bool(user_info.get("verified_email")),
        "name": str(user_info.get("name") or "").strip(),
    }
