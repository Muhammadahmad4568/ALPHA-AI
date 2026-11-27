"""
tools/gmail_tool.py - send email through Gmail (single or bulk) for Alpha.

send_bulk_emails is ONE tool call that loops over every recipient itself (ctx.step shows live
progress on the dashboard, one line per recipient) - so "email all 80 people in my sheet" is one
LLM round-trip plus 80 Gmail API calls, not 80 separate LLM round-trips.
"""
from __future__ import annotations

import base64
import logging
import time
from email.mime.text import MIMEText

from core.registry import tool
from tools.google_client import GoogleAuthError, gmail_service

log = logging.getLogger("alpha.gmail")


def _send_one(to: str, subject: str, body: str) -> None:
    msg = MIMEText(body)
    msg["to"] = to
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail_service().users().messages().send(userId="me", body={"raw": raw}).execute()


def _on(svc) -> bool:
    return True  # self-contained: needs nothing from core.registry.Services


@tool("send_email", "Send a single email through Gmail. Always confirms the recipient and subject "
      "with the user first.",
      params={"to": {"type": "string", "description": "Recipient email address"},
              "subject": {"type": "string"}, "body": {"type": "string"}},
      required=["to", "subject", "body"], agent="Email", available=_on, confirm=True,
      confirm_text=lambda a: f"Send this email to {a.get('to', '')}?\nSubject: {a.get('subject', '')}\n"
                             "Please say yes or no.",
      title=lambda a: f"Email to {a.get('to', '')}")
def send_email(ctx, to: str, subject: str, body: str) -> str:
    try:
        ctx.step(f"Sending to {to}")
        _send_one(to, subject, body)
        return f"Email sent to {to}."
    except GoogleAuthError as exc:
        return str(exc)
    except Exception as exc:
        log.exception("send_email failed")
        return f"That action failed: {exc}"


# FIX: "recipients" used to be a JSON array ({"type":"array","items":{"type":"string"}}). That was
# the only array-typed tool parameter in the whole project, and Gemini's request validation rejected
# the ENTIRE function-calling request over it ("...items: missing field"), which broke every tool in
# the same request, not just this one. A plain comma-separated string avoids the array schema
# entirely and is just as easy for the model to produce.
@tool("send_bulk_emails", "Send the SAME email to a list of recipients at once (e.g. 80 addresses "
      "from a Google Sheet). Confirms once for the whole batch, then sends every one without asking "
      "again per person.",
      params={"recipients": {"type": "string", "description": "Recipient email addresses, separated by commas"},
              "subject": {"type": "string"}, "body": {"type": "string"}},
      required=["recipients", "subject", "body"], agent="Email", available=_on, confirm=True,
      confirm_text=lambda a: f"Send this email to {len((a.get('recipients') or '').split(','))} people? "
                             f"Subject: {a.get('subject', '')}\nPlease say yes or no.",
      title=lambda a: "Bulk email")
def send_bulk_emails(ctx, recipients: str, subject: str, body: str) -> str:
    recipients = [r.strip() for r in (recipients or "").split(",") if r.strip()]
    if not recipients:
        return "I need at least one recipient address."
    sent, failed = [], []
    for i, to in enumerate(recipients, 1):
        ctx.step(f"Sending {i}/{len(recipients)}: {to}")
        try:
            _send_one(to, subject, body)
            sent.append(to)
        except GoogleAuthError as exc:
            return str(exc)  # a setup problem - stop the whole batch, don't retry 80 times
        except Exception as exc:
            failed.append(to)
            log.warning("Email to %s failed: %s", to, exc)
        time.sleep(0.3)  # be gentle with Gmail's per-second send quota
    result = f"Sent {len(sent)}/{len(recipients)} emails."
    if failed:
        shown = ", ".join(failed[:10]) + ("..." if len(failed) > 10 else "")
        result += f" Failed for: {shown}"
    return result