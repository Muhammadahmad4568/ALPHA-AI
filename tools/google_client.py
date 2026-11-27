"""
tools/google_client.py - shared Google OAuth client for the Gmail and Sheets tools.

First time any Gmail/Sheets tool runs, this opens your browser ONCE for consent (same pattern as
WhatsApp's QR-code login). The resulting token is cached at data/google_token.json, so every run
after that is silent - no browser, no re-login.

SETUP (one-time, do this before testing):
  1. config/credentials.json must exist (the OAuth "Desktop app" client JSON you downloaded).
  2. Your OAuth consent screen is in "Testing" mode (normal for personal projects), which means
     ONLY accounts listed as test users can log in. Go to Google Auth Platform -> Audience -> add
     your own Gmail address under "Test users". Skipping this gives an "Access blocked" error.
  3. pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
"""
from __future__ import annotations

import logging
from pathlib import Path

from config.settings import DATA_DIR, ROOT

log = logging.getLogger("alpha.google")

# gmail.send only (not full mailbox access) + spreadsheets (read/write). Minimal scopes = less to
# worry about if the token ever leaks.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/spreadsheets",
]
CREDENTIALS_FILE = ROOT / "config" / "credentials.json"
TOKEN_FILE = DATA_DIR / "google_token.json"


class GoogleAuthError(Exception):
    """Raised for setup problems (missing credentials.json, consent not granted, etc) - these should
    stop a whole bulk operation immediately rather than being retried per-recipient."""


_creds = None
_gmail = None
_sheets = None


def _load_creds():
    global _creds
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    if _creds and _creds.valid:
        return _creds

    creds = None
    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        except Exception:
            log.warning("Stored Google token is unreadable; re-authenticating.", exc_info=True)
            creds = None

    if creds and creds.valid:
        _creds = creds
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
            _creds = creds
            return creds
        except Exception:
            log.warning("Google token refresh failed; asking for consent again.", exc_info=True)

    if not CREDENTIALS_FILE.exists():
        raise GoogleAuthError(
            f"Google isn't set up yet: {CREDENTIALS_FILE} is missing. Download your OAuth Desktop "
            "app client's JSON from Google Cloud Console (Clients page) and save it at that path.")

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)          # opens the browser once for you to sign in
    except Exception as exc:
        raise GoogleAuthError(
            f"Google sign-in failed ({exc}). If you see 'Access blocked', add your Gmail address as "
            "a test user under Google Auth Platform -> Audience.")

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json())
    _creds = creds
    return creds


def gmail_service():
    global _gmail
    if _gmail is None:
        from googleapiclient.discovery import build

        _gmail = build("gmail", "v1", credentials=_load_creds())
    return _gmail


def sheets_service():
    global _sheets
    if _sheets is None:
        from googleapiclient.discovery import build

        _sheets = build("sheets", "v4", credentials=_load_creds())
    return _sheets