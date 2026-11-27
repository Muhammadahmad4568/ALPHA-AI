"""
n8n_client.py - send structured webhooks from Alpha to n8n workflows.

Configure in .env (see .env.example):
    N8N_WORKFLOWS   JSON {"email_draft": "http://localhost:5678/webhook/email-draft", ...}
                    value may also be {"url": "...", "description": "...", "confirm": false}
                    or the short form  name=url;name2=url2
    N8N_WEBHOOK_URL one catch-all webhook (registered as workflow "default")
    N8N_WEBHOOK_<NAME>=url  one variable per workflow
    N8N_AUTH_HEADER / N8N_AUTH_VALUE   for n8n's "Header Auth" (default header: Authorization)
    N8N_BASIC_USER / N8N_BASIC_PASS    for n8n's "Basic auth"
    N8N_TIMEOUT (30 s), N8N_RETRIES (2), N8N_VERIFY_TLS (1), N8N_CONFIRM (1)

SECURITY: the AI model can only pick a workflow *name* from this allow-list; it can never
supply a URL, so a prompt-injected instruction cannot send your data to an arbitrary server.
By default every workflow run also asks you for yes/no confirmation first.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests

log = logging.getLogger("alpha.n8n")


@dataclass
class N8nResult:
    ok: bool
    status: int = 0
    text: str = ""
    data: Any = None
    ms: int = 0
    workflow: str = ""
    request_id: str = ""

    def speakable(self) -> str:
        if not self.ok:
            return self.text
        if isinstance(self.data, dict):
            for key in ("message", "result", "text", "output", "reply", "summary"):
                if isinstance(self.data.get(key), (str, int, float)) and str(self.data[key]).strip():
                    return str(self.data[key]).strip()[:400]
        if isinstance(self.data, list) and self.data and isinstance(self.data[0], dict):
            return N8nResult(True, data=self.data[0]).speakable()
        return self.text or f"The {self.workflow} workflow ran successfully."


@dataclass
class Workflow:
    name: str
    url: str
    description: str = ""
    confirm: bool = True


class N8nClient:
    def __init__(self, hub=None, on_result: Optional[Callable[[N8nResult], None]] = None):
        env = os.environ.get
        self.hub, self.on_result = hub, on_result
        self.timeout = float(env("N8N_TIMEOUT", "30") or 30)
        self.retries = int(env("N8N_RETRIES", "2") or 2)
        self.verify_tls = env("N8N_VERIFY_TLS", "1") != "0"
        self.confirm_default = env("N8N_CONFIRM", "1") != "0"
        self._headers = {"Content-Type": "application/json", "User-Agent": "alpha-ai/3"}
        if env("N8N_AUTH_VALUE"):
            self._headers[env("N8N_AUTH_HEADER", "Authorization") or "Authorization"] = env("N8N_AUTH_VALUE", "")
        self._basic = (env("N8N_BASIC_USER"), env("N8N_BASIC_PASS", "")) if env("N8N_BASIC_USER") else None
        self.workflows: Dict[str, Workflow] = self._load_workflows()
        self._http = requests.Session()

    # ------------------------------------------------------------- config
    def _add(self, wf: Dict[str, Workflow], name: str, spec: Any) -> None:
        name = name.strip().lower().replace(" ", "_").replace("-", "_")
        if not name:
            return
        if isinstance(spec, str):
            spec = {"url": spec}
        if not isinstance(spec, dict) or not str(spec.get("url", "")).startswith(("http://", "https://")):
            log.warning("Ignoring n8n workflow '%s': it needs an http(s) URL.", name)
            return
        wf[name] = Workflow(name, spec["url"].strip(), str(spec.get("description", "")),
                            bool(spec.get("confirm", self.confirm_default)))

    def _load_workflows(self) -> Dict[str, Workflow]:
        wf: Dict[str, Workflow] = {}
        raw = os.environ.get("N8N_WORKFLOWS", "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                for name, spec in (parsed.items() if isinstance(parsed, dict) else []):
                    self._add(wf, name, spec)
            except ValueError:  # name=url;name2=url2
                for pair in raw.split(";"):
                    if "=" in pair:
                        name, _, url = pair.partition("=")
                        self._add(wf, name, url.strip())
        for key, value in os.environ.items():
            if key.startswith("N8N_WEBHOOK_") and key != "N8N_WEBHOOK_URL" and value.strip():
                self._add(wf, key[len("N8N_WEBHOOK_"):], value.strip())
        if os.environ.get("N8N_WEBHOOK_URL", "").strip():
            self._add(wf, "default", os.environ["N8N_WEBHOOK_URL"].strip())
        return wf

    @property
    def enabled(self) -> bool:
        return bool(self.workflows)

    def names(self) -> List[str]:
        return sorted(self.workflows)

    def requires_confirmation(self, name: str) -> bool:
        wf = self.workflows.get(self._norm(name))
        return True if wf is None else wf.confirm

    @staticmethod
    def _norm(name: str) -> str:
        return (name or "").strip().lower().replace(" ", "_").replace("-", "_")

    def describe(self) -> str:
        return "; ".join(f"{w.name}" + (f" ({w.description})" if w.description else "") for w in self.workflows.values())

    # ------------------------------------------------------------- calls
    def trigger(self, workflow: str, instruction: str = "", data: Optional[dict] = None) -> N8nResult:
        """POST a structured payload and wait for the answer (blocking; retries on 5xx / connection errors)."""
        name = self._norm(workflow)
        wf = self.workflows.get(name)
        if wf is None:
            known = ", ".join(self.names()) or "none configured"
            return N8nResult(False, text=f"I don't have an n8n workflow called {workflow}. Known workflows: {known}.",
                             workflow=name)
        request_id = uuid.uuid4().hex[:12]
        payload = {
            "event": "alpha.workflow",
            "workflow": name,
            "request_id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "alpha-ai",
            "instruction": instruction,
            "data": data or {},
        }
        host = urlparse(wf.url).netloc
        last = ""
        t0 = time.perf_counter()
        for attempt in range(self.retries + 1):
            try:
                resp = self._http.post(wf.url, json=payload, headers=self._headers, auth=self._basic,
                                       timeout=(5, self.timeout), verify=self.verify_tls)
            except requests.RequestException as exc:
                last = f"I couldn't reach n8n at {host}: {type(exc).__name__}"
                log.warning("n8n request failed (attempt %d): %s", attempt + 1, exc)
                time.sleep(min(1.0 * (attempt + 1), 3))
                continue
            ms = int((time.perf_counter() - t0) * 1000)
            if resp.status_code < 300:
                try:
                    body: Any = resp.json()
                except ValueError:
                    body = None
                text = "" if body is not None else resp.text.strip()[:2000]
                return N8nResult(True, resp.status_code, text, body, ms, name, request_id)
            if resp.status_code in (401, 403):
                return N8nResult(False, resp.status_code, "n8n rejected the request: check N8N_AUTH_HEADER and "
                                 "N8N_AUTH_VALUE match the webhook's Header Auth credential.", None, ms, name, request_id)
            if resp.status_code == 404:
                return N8nResult(False, 404, "n8n says that webhook doesn't exist. Make sure the workflow is active "
                                 "and the URL is the production one (/webhook/...), not /webhook-test/.",
                                 None, ms, name, request_id)
            last = f"n8n answered with HTTP {resp.status_code}."
            if resp.status_code < 500:
                return N8nResult(False, resp.status_code, last, None, ms, name, request_id)
            time.sleep(min(1.0 * (attempt + 1), 3))
        return N8nResult(False, 0, last or "n8n did not respond.", None, int((time.perf_counter() - t0) * 1000),
                         name, request_id)

    def trigger_async(self, workflow: str, instruction: str = "", data: Optional[dict] = None) -> None:
        """Fire-and-forget: runs on the hub's 'net' queue and reports through on_result."""
        def job() -> None:
            result = self.trigger(workflow, instruction, data)
            if self.on_result:
                self.on_result(result)

        if self.hub is not None:
            self.hub.submit("net", job)
        else:
            import threading

            threading.Thread(target=job, daemon=True, name="n8n-async").start()
