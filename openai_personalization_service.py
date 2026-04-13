"""
OpenAI-powered email personalization helpers.
"""
import json
import os
from typing import Dict, Optional

from openai import OpenAI


SYSTEM_PROMPT = """
You write concise, professional outbound emails for business recipients.
Use only the recipient data and campaign details provided.
Do not invent projects, funding, partnerships, or recent news.
Keep the email specific, natural, and short enough to feel human-written.
Preserve the sender's core offer and call to action.
Return valid JSON only.
""".strip()


def _safe_str(value: Optional[object]) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OpenAI not configured. Set OPENAI_API_KEY in .env")
    return OpenAI(api_key=api_key)


def personalize_email(
    *,
    recipient_email: str,
    recipient_name: str,
    recipient_title: str,
    recipient_company: str,
    base_subject: str,
    base_html: str,
    base_text: str,
) -> Dict[str, str]:
    """
    Generate a tailored email draft for a single recipient.
    """
    prompt = f"""
Recipient details:
- Email: {_safe_str(recipient_email)}
- Name: {_safe_str(recipient_name)}
- Title: {_safe_str(recipient_title)}
- Company: {_safe_str(recipient_company)}

Base subject:
{_safe_str(base_subject)}

Base HTML template:
{_safe_str(base_html)}

Base plain text template:
{_safe_str(base_text)}

Instructions:
- Tailor the email to the recipient using only the provided fields.
- If a field is missing, do not mention it.
- Keep the same overall offer and CTA.
- Keep the tone professional and natural.
- Return JSON with keys: subject, html_body, text_body.
""".strip()

    response = _client().responses.create(
        model=os.getenv("OPENAI_PERSONALIZATION_MODEL", "gpt-5-mini"),
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )

    content = (getattr(response, "output_text", "") or "").strip()
    if not content:
        raise RuntimeError("OpenAI returned an empty personalization response.")

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OpenAI returned non-JSON personalization output: {exc}") from exc

    subject = _safe_str(parsed.get("subject"))
    html_body = _safe_str(parsed.get("html_body"))
    text_body = _safe_str(parsed.get("text_body"))

    if not subject or not html_body or not text_body:
        raise RuntimeError("OpenAI response was missing one or more required fields.")

    return {
        "subject": subject,
        "html_body": html_body,
        "text_body": text_body,
    }
