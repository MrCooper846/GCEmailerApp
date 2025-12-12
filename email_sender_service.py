"""
Email sending service - adapted from emailerv2.py for web app use
"""
import os
import ssl
import logging
import smtplib
from time import sleep
from smtplib import SMTPResponseException
from email.message import EmailMessage
import pandas as pd
from typing import List, Optional


def build_message(to_addr: str, first_name: str, subject: str, 
                  html_content: str, text_content: str, email_from: str) -> EmailMessage:
    """
    Build an EmailMessage with personalization
    """
    name = first_name.strip() or "Sir/Madam"
    
    # Personalize subject, HTML and text
    subject_personalized = subject.replace("{{FirstName}}", name).replace("{{Name}}", name)
    html = html_content.replace("{{FirstName}}", name).replace("{{Name}}", name)
    text = text_content.replace("{{FirstName}}", name).replace("{{Name}}", name)

    # Create email
    msg = EmailMessage()
    msg["From"] = email_from
    msg["To"] = to_addr
    msg["Subject"] = subject_personalized

    # HTML as primary content
    msg.set_content(html, subtype='html')
    
    # Plain-text alternative (set as alternative)
    msg.add_alternative(text, subtype='plain')
   
    return msg


def send_email_campaign(df: pd.DataFrame,
                       email_col: str,
                       name_col: Optional[str],
                       subject: str,
                       html_content: str,
                       text_content: str,
                       smtp_user: str,
                       smtp_pass: str,
                       smtp_host: str = "smtp.gmail.com",
                       smtp_port: int = 465,
                       base_delay: float = 0.1,
                       max_delay: float = 10,
                       progress_callback=None) -> dict:
    """
    Send personalized emails to a list
    
    Args:
        df: DataFrame with recipient data
        email_col: Column name containing email addresses
        name_col: Column name containing recipient names (optional)
        subject: Email subject line
        html_content: HTML email body (can use {{FirstName}} or {{Name}} placeholders)
        text_content: Plain text fallback
        smtp_user: SMTP username
        smtp_pass: SMTP password
        smtp_host: SMTP server hostname
        smtp_port: SMTP port (465 for SSL)
        base_delay: Base delay between emails in seconds
        max_delay: Maximum delay for backoff
        progress_callback: Optional function(sent, total, status) for progress updates
    
    Returns:
        dict with keys: sent, failed, errors (list of error messages)
    """
    if not smtp_user or not smtp_pass:
        raise ValueError("SMTP credentials required")

    results = {
        "sent": 0,
        "failed": 0,
        "errors": []
    }

    messages: List[EmailMessage] = []
    
    # Build all messages
    for idx, row in df.iterrows():
        email = str(row[email_col]).strip()
        if not email:
            continue
            
        first_name = ""
        if name_col and name_col in df.columns:
            first_name = str(row[name_col]).strip()
        
        try:
            msg = build_message(email, first_name, subject, html_content, 
                              text_content, smtp_user)
            messages.append(msg)
        except Exception as e:
            results["errors"].append(f"Failed to build message for {email}: {e}")
            results["failed"] += 1

    if progress_callback:
        progress_callback(0, len(messages), "Connecting to SMTP server...")

    # Send messages
    context = ssl.create_default_context()
    delay = base_delay
    
    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
            server.login(smtp_user, smtp_pass)
            
            for i, msg in enumerate(messages):
                try:
                    server.send_message(msg)
                    results["sent"] += 1
                    delay = max(base_delay, delay * 0.9)
                    
                    if progress_callback:
                        progress_callback(results["sent"], len(messages), 
                                        f"Sent to {msg['To']}")
                    
                except SMTPResponseException as e:
                    code = e.smtp_code
                    if code in (421, 450, 451, 452):
                        # Throttled - back off and retry
                        sleep(delay)
                        delay = min(max_delay, delay * 2)
                        try:
                            server.send_message(msg)
                            results["sent"] += 1
                            
                            if progress_callback:
                                progress_callback(results["sent"], len(messages),
                                                f"Retry succeeded for {msg['To']}")
                        except Exception as e2:
                            results["failed"] += 1
                            results["errors"].append(f"Retry failed for {msg['To']}: {e2}")
                    else:
                        results["failed"] += 1
                        results["errors"].append(f"Failed to send to {msg['To']}: {e}")
                        
                except Exception as e:
                    results["failed"] += 1
                    results["errors"].append(f"Error sending to {msg['To']}: {e}")
                
                sleep(delay)
                
    except Exception as e:
        results["errors"].append(f"SMTP connection error: {e}")
        return results

    if progress_callback:
        progress_callback(results["sent"], len(messages), 
                         f"Complete! Sent: {results['sent']}, Failed: {results['failed']}")

    return results
