"""
tools/whatsapp_tool.py - send WhatsApp messages through WhatsApp Web (Playwright).

How it works
  * One persistent browser profile (data/whatsapp_profile): you scan the QR code ONCE; later runs are already
    logged in. It uses the Microsoft Edge / Chrome already installed on Windows (no browser download); the
    bundled Chromium (`playwright install chromium`) is the last fallback.
  * The browser lives in its own worker thread (Playwright objects are thread-bound) and stays open between
    messages; it closes itself after WHATSAPP_IDLE_CLOSE seconds idle (the login is kept).
  * Contacts come from config/contacts.json (name -> phone or {"chat": "Group name"}). Unknown names are refused
    instead of guessed, so a misheard name can never message a stranger. A raw phone number also works.
  * EVERY send asks you for yes/no first (the confirmation shows the recipient and the full text).
  * Limits: WHATSAPP_MAX_PER_HOUR (10) and WHATSAPP_MIN_GAP seconds (8) between messages.

IMPORTANT: WhatsApp's terms restrict unofficial automation and accounts that send automated or bulk messages can
be banned. Use it for your own low-volume personal messages. For business use, use the official WhatsApp Business
Cloud API. WhatsApp Web's page structure changes; selectors below try several alternatives, but a WhatsApp update can
still break them (fix the lists in COMPOSER / SEARCH / MSG_OUT).
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
from urllib.parse import quote

from config.settings import CONTACTS_FILE, WHATSAPP_PROFILE, env_bool, env_float, env_int
from core.registry import tool

log = logging.getLogger("alpha.whatsapp")

COMPOSER = ['footer div[contenteditable="true"][role="textbox"]', 'div[contenteditable="true"][data-tab="10"]',
            'footer div[contenteditable="true"]', 'div[contenteditable="true"][aria-label*="message" i]']
SEARCH = ['div[contenteditable="true"][data-tab="3"]', 'div[role="textbox"][aria-label*="Search" i]',
          'input[aria-label*="Search" i]']
LOGGED_IN = ['#pane-side', 'div[aria-label="Chat list"]']
MSG_OUT = 'div.message-out, div[data-testid="msg-container"].message-out'
PENDING_ICON = 'span[data-icon="msg-time"]'
QR = ['canvas[aria-label*="QR" i]', 'div[data-ref]']
INVALID = re.compile(r"phone number shared via url is invalid|isn't on whatsapp|not on whatsapp", re.I)


class WhatsAppError(Exception):
    pass


@dataclass
class Target:
    kind: str       # "phone" | "chat"
    value: str
    display: str


def normalize_phone(raw: str, default_country: str = "") -> str:
    digits = re.sub(r"[^\d+]", "", raw or "")
    plus = digits.startswith("+")
    digits = digits.lstrip("+")
    if not plus:
        if digits.startswith("00"):
            digits = digits[2:]
        elif digits.startswith("0"):
            if not default_country:
                raise WhatsAppError(f"'{raw}' is a local number. Use the international format (+92...) or set "
                                    "WHATSAPP_DEFAULT_COUNTRY (for example 92) in your .env.")
            digits = default_country + digits[1:]
    if not 8 <= len(digits) <= 15:
        raise WhatsAppError(f"'{raw}' doesn't look like a full phone number with country code.")
    return digits


def load_contacts(path=CONTACTS_FILE) -> Dict[str, object]:
    try:
        data = json.loads(open(path, encoding="utf-8-sig").read())
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise WhatsAppError(f"contacts.json is not valid JSON: {exc}")
    return {str(k).strip().lower(): v for k, v in data.items() if not str(k).startswith("_")}


def resolve_target(who: str, contacts: Dict[str, object], default_country: str = "") -> Target:
    who = (who or "").strip()
    if not who:
        raise WhatsAppError("Who should I send it to?")
    if re.fullmatch(r"[+\d][\d\s\-().]{6,}", who):
        return Target("phone", normalize_phone(who, default_country), who)
    key = who.lower()
    hit = contacts.get(key)
    if hit is None:
        cands = [k for k in contacts if key in k or k in key]
        if len(cands) > 1:
            raise WhatsAppError(f"'{who}' matches several contacts ({', '.join(sorted(cands)[:4])}). Please say which one.")
        hit = contacts[cands[0]] if cands else None
        key = cands[0] if cands else key
    if hit is None:
        raise WhatsAppError(f"I don't have a contact called {who}. Add it to config/contacts.json, or give a phone number.")
    if isinstance(hit, dict):
        if hit.get("chat"):
            return Target("chat", str(hit["chat"]), who)
        hit = hit.get("phone", "")
    return Target("phone", normalize_phone(str(hit), default_country), who)


class WhatsAppClient:
    def __init__(self, profile_dir=WHATSAPP_PROFILE, contacts_file=CONTACTS_FILE, browser_factory: Optional[Callable] = None):
        self.profile_dir, self.contacts_file = profile_dir, contacts_file
        self.base_url = os.environ.get("WHATSAPP_URL", "https://web.whatsapp.com").rstrip("/")
        self.headless = env_bool("WHATSAPP_HEADLESS", False)
        self.browser_pref = os.environ.get("WHATSAPP_BROWSER", "msedge")
        self.default_country = os.environ.get("WHATSAPP_DEFAULT_COUNTRY", "").strip()
        self.max_per_hour = env_int("WHATSAPP_MAX_PER_HOUR", 10)
        self.min_gap = env_float("WHATSAPP_MIN_GAP", 8.0)
        self.idle_close = env_float("WHATSAPP_IDLE_CLOSE", 600.0)
        self.login_timeout = env_float("WHATSAPP_LOGIN_TIMEOUT", 120.0)
        self.enabled = env_bool("WHATSAPP_ENABLED", True)
        self._factory = browser_factory
        self._jobs: "queue.Queue" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._sent: "deque[float]" = deque()
        self._pw = self._ctx = self._page = None

    # ------------------------------------------------------------------ API
    def send(self, who: str, message: str, progress: Callable[[str], object] = lambda s: None) -> str:
        message = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", (message or "")).strip()
        if not message:
            return "What should the message say?"
        if len(message) > 1500:
            return "That message is too long for me to send safely (limit 1500 characters)."
        progress("Resolving the contact")
        try:
            target = resolve_target(who, load_contacts(self.contacts_file), self.default_country)
        except WhatsAppError as exc:
            return str(exc)
        wait = self._rate_limit_wait()
        if wait:
            return wait
        fut: "concurrent.futures.Future" = concurrent.futures.Future()
        self._ensure_thread()
        self._jobs.put((target, message, progress, fut))
        try:
            result = fut.result(timeout=self.login_timeout + 90)
        except concurrent.futures.TimeoutError:
            return "WhatsApp took too long to respond, so I stopped. Check the WhatsApp window."
        except WhatsAppError as exc:
            return str(exc)
        except Exception as exc:
            log.exception("WhatsApp send failed")
            return f"WhatsApp automation failed: {type(exc).__name__}: {str(exc)[:160]}"
        with self._lock:
            self._sent.append(time.time())
        return result

    def _rate_limit_wait(self) -> str:
        with self._lock:
            now = time.time()
            while self._sent and now - self._sent[0] > 3600:
                self._sent.popleft()
            if len(self._sent) >= self.max_per_hour:
                return f"I've already sent {self.max_per_hour} WhatsApp messages in the last hour, so I'm pausing to protect your account."
            if self._sent and now - self._sent[-1] < self.min_gap:
                return f"Please wait {int(self.min_gap - (now - self._sent[-1])) + 1} seconds before another WhatsApp message."
        return ""

    def shutdown(self) -> None:
        if self._thread and self._thread.is_alive():
            self._jobs.put(None)
            self._thread.join(timeout=8)

    # --------------------------------------------------------------- worker
    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, name="WhatsApp", daemon=True)
                self._thread.start()

    def _worker(self) -> None:
        last_used = time.time()
        while True:
            try:
                job = self._jobs.get(timeout=30)
            except queue.Empty:
                if self._page is not None and time.time() - last_used > self.idle_close:
                    log.info("Closing the idle WhatsApp browser (login is kept).")
                    self._close_browser()
                continue
            if job is None:
                break
            target, message, progress, fut = job
            try:
                fut.set_result(self._send_now(target, message, progress))
            except BaseException as exc:  # noqa: BLE001 - delivered to the caller
                fut.set_exception(exc)
            last_used = time.time()
        self._close_browser()

    def _close_browser(self) -> None:
        for obj, method in ((self._ctx, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, method)()
            except Exception:
                pass
        self._pw = self._ctx = self._page = None

    def _open_browser(self, progress) -> None:
        if self._page is not None:
            try:
                if not self._page.is_closed():
                    return
            except Exception:
                pass
            self._close_browser()
        if self._factory:
            self._ctx, self._page = self._factory()
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise WhatsAppError("WhatsApp automation needs Playwright: run pip install playwright")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        progress("Launching the browser (Playwright)")
        self._pw = sync_playwright().start()
        last: Optional[Exception] = None
        for channel in dict.fromkeys([self.browser_pref, "msedge", "chrome", None]):
            try:
                self._ctx = self._pw.chromium.launch_persistent_context(
                    str(self.profile_dir), channel=channel, headless=self.headless,
                    viewport={"width": 1100, "height": 800}, args=["--disable-blink-features=AutomationControlled"])
                log.info("WhatsApp browser: %s", channel or "bundled chromium")
                break
            except Exception as exc:
                last = exc
                log.info("Browser channel %s unavailable: %s", channel, str(exc).splitlines()[0][:100])
        else:
            self._close_browser()
            raise WhatsAppError("I couldn't start a browser. Install Microsoft Edge or Chrome, or run: playwright install chromium"
                                f" ({str(last).splitlines()[0][:80] if last else ''})")
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    # ---------------------------------------------------------------- steps
    @staticmethod
    def _first_visible(page, selectors: List[str], timeout_ms: int):
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            for sel in selectors:
                loc = page.locator(sel).first
                try:
                    if loc.is_visible():
                        return loc
                except Exception:
                    pass
            page.wait_for_timeout(250)
        return None

    def _wait_logged_in(self, page, progress) -> None:
        progress("Opening WhatsApp Web")
        if self.base_url not in (page.url or ""):
            page.goto(self.base_url + "/", wait_until="domcontentloaded", timeout=45000)
        if self._first_visible(page, LOGGED_IN, 15000):
            return
        if self._first_visible(page, QR, 3000):
            progress("Scan the QR code in the WhatsApp window (needed once)")
            if self.headless:
                raise WhatsAppError("WhatsApp needs a QR login. Run once with WHATSAPP_HEADLESS=0, scan the code, then retry.")
        if not self._first_visible(page, LOGGED_IN, int(self.login_timeout * 1000)):
            raise WhatsAppError("WhatsApp Web didn't finish loading (QR not scanned?).")

    def _send_now(self, target: Target, message: str, progress) -> str:
        self._open_browser(progress)
        page = self._page
        self._wait_logged_in(page, progress)
        if target.kind == "phone":
            progress(f"Opening the chat with {target.display}")
            page.goto(f"{self.base_url}/send?phone={target.value}&text={quote(message)}", wait_until="domcontentloaded", timeout=45000)
            composer = self._first_visible(page, COMPOSER, 30000)
            if composer is None:
                body = ""
                try:
                    body = page.inner_text("body")[:600]
                except Exception:
                    pass
                if INVALID.search(body):
                    raise WhatsAppError(f"WhatsApp says {target.display} isn't a valid WhatsApp number.")
                raise WhatsAppError("I couldn't find the message box. WhatsApp Web may have changed.")
            progress("Checking the message text")
            if message[:15].lower() not in (composer.inner_text() or "").lower():
                composer.click()
                page.keyboard.insert_text(message)
        else:
            progress(f"Searching for the chat '{target.value}'")
            box = self._first_visible(page, SEARCH, 10000)
            if box is None:
                raise WhatsAppError("I couldn't find WhatsApp's search box.")
            box.click()
            page.keyboard.insert_text(target.value)
            page.wait_for_timeout(1200)
            hit = page.locator(f'#pane-side span[title="{target.value}"]').first
            if hit.count() == 0:
                raise WhatsAppError(f"I couldn't find a chat called {target.value}.")
            hit.click()
            progress("Typing the message")
            composer = self._first_visible(page, COMPOSER, 15000)
            if composer is None:
                raise WhatsAppError("I couldn't find the message box.")
            composer.click()
            page.keyboard.insert_text(message)
        baseline = page.locator(MSG_OUT).count()   # measured now: opening the chat reloads the message list
        progress("Sending")
        composer.press("Enter")
        return self._verify(page, message, baseline, progress, target.display)

    def _verify(self, page, message: str, baseline: int, progress, display: str) -> str:
        progress("Verifying delivery to WhatsApp's server")
        snippet = message[:30].lower()
        deadline = time.time() + 20
        seen = False
        while time.time() < deadline:
            out = page.locator(MSG_OUT)
            if out.count() > baseline:
                last = out.last
                try:
                    if snippet in (last.inner_text() or "").lower():
                        seen = True
                        if last.locator(PENDING_ICON).count() == 0:
                            return f"Message sent to {display}."
                except Exception:
                    pass
            page.wait_for_timeout(400)
        if seen:
            return f"The message to {display} is queued, but WhatsApp hasn't confirmed it yet; check your internet connection."
        return f"I pressed send for {display}, but couldn't verify it. Please check the chat."


# ------------------------------------------------------------------ the tool
def _on(svc) -> bool:
    return bool(getattr(svc, "whatsapp", None) and svc.whatsapp.enabled)


@tool("send_whatsapp", "Send a WhatsApp message to a saved contact (or a phone number with country code) through "
      "WhatsApp Web. The user is asked to confirm the recipient and text first.",
      params={"contact": {"type": "string", "description": "Contact name from contacts.json, or a phone number"},
              "message": {"type": "string", "description": "The exact text to send"}},
      required=["contact", "message"], agent="Messaging", available=_on, confirm=True,
      confirm_text=lambda a: f"Send this WhatsApp message to {a.get('contact', '')}: \u201c{str(a.get('message', ''))[:200]}\u201d? "
                             "Please say yes or no.",
      title=lambda a: f"WhatsApp to {a.get('contact', '')}")
def send_whatsapp(ctx, contact: str, message: str) -> str:
    return ctx.svc.whatsapp.send(contact, message, progress=ctx.step)
