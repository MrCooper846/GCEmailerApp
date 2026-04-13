"""
Send personalized emails via Gmail API using OAuth credentials.
"""
import base64
import logging
from typing import Optional

import pandas as pd
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request

from email_sender_service import build_message


def _encode_message(msg) -> str:
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def send_email_campaign_gmail(
    df: pd.DataFrame,
    email_col: str,
    name_col: Optional[str],
    subject: str,
    html_content: str,
    text_content: str,
    credentials,
    progress_callback=None,
) -> dict:
    if not credentials:
        raise ValueError("Missing Google credentials")

    # Refresh if needed
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    results = {"sent": 0, "failed": 0, "errors": []}
    messages = []

    for _, row in df.iterrows():
        email = str(row[email_col]).strip()
        if not email:
            continue
        first_name = ""
        if name_col and name_col in df.columns:
            first_name = str(row[name_col]).strip()
        row_subject = str(row.get("ai_subject", "")).strip() or subject
        row_html = str(row.get("ai_html_content", "")).strip() or html_content
        row_text = str(row.get("ai_text_content", "")).strip() or text_content
        try:
            msg = build_message(email, first_name, row_subject, row_html, row_text, email_from="me")
            messages.append(msg)
        except Exception as e:
            results["failed"] += 1
            results["errors"].append(f"Failed to build message for {email}: {e}")

    total = len(messages)
    if progress_callback:
        progress_callback(0, total, "Connecting to Gmail API...")

    for idx, msg in enumerate(messages, 1):
        try:
            raw = _encode_message(msg)
            service.users().messages().send(userId="me", body={"raw": raw}).execute()
            results["sent"] += 1
            if progress_callback:
                progress_callback(results["sent"], total, f"Sent to {msg['To']}")
        except HttpError as e:
            results["failed"] += 1
            results["errors"].append(f"HTTP {e.resp.status} for {msg['To']}: {e}")
        except Exception as e:
            results["failed"] += 1
            results["errors"].append(f"Error sending to {msg['To']}: {e}")

    if progress_callback:
        progress_callback(results["sent"], total, "Complete")

    return results
