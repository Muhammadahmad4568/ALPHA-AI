"""
brains.py - the language models behind Alpha's Reasoning Agent.

  GeminiBrain  Google Gemini over REST. Streams the answer (so speech can start after the
               first sentence) and does function calling. Rotates to the next model on 404
               (retired) or 429 (per-model free-tier quota) and can discover current Flash
               models. gemini-1.5-flash is retired and intentionally not used.
  ClaudeBrain  Anthropic Messages API (optional, non-streaming).
  OfflineBrain placeholder when no key exists: the orchestrator uses its rule router.

All brains share one call:
    respond(user_text, tools, run_tool, system, on_text=None, on_first_token=None) -> Reply
`tools` use the neutral schema {name, description, input_schema}; `run_tool(name, args) -> str`
executes a call through the orchestrator (which handles confirmation).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import requests

log = logging.getLogger("alpha.brain")

CLAUDE_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5-20251001"
GEMINI_ROOT = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_DEFAULT_MODELS = ["gemini-3.5-flash", "gemini-3.8-flash", "gemini-3.1-flash-lite", "gemini-3-flash-preview"]

RunTool = Callable[[str, dict], str]


class BrainError(Exception):
    pass


@dataclass
class Reply:
    text: str
    spoken: bool = False   # True when `text` was already streamed to the speech pipeline


def clean_key(value: Optional[str]) -> str:
    """Treat empty values and copy-pasted placeholders as 'no key'."""
    v = (value or "").strip()
    if not v or v.lower().startswith(("paste", "your", "<", "sk-ant-your", "xxx")):
        return ""
    return v


def _gemini_schema(node: dict) -> dict:
    return {k: node[k] for k in ("type", "description", "enum", "minimum", "maximum") if k in node}


def gemini_declarations(tools: List[dict]) -> List[dict]:
    """Neutral tool schema -> Gemini functionDeclarations (parameter-less tools omit `parameters`)."""
    decls = []
    for t in tools:
        schema = t["input_schema"]
        decl = {"name": t["name"], "description": t["description"]}
        props = schema.get("properties") or {}
        if props:
            params = {"type": "object", "properties": {k: _gemini_schema(v) for k, v in props.items()}}
            if schema.get("required"):
                params["required"] = list(schema["required"])
            decl["parameters"] = params
        decls.append(decl)
    return decls


def _gemini_model_rank(name: str) -> tuple:
    m = re.search(r"gemini-(\d+(?:\.\d+)?)-flash(-lite)?", name)
    version = float(m.group(1)) if m else 0.0
    unstable = 1 if ("preview" in name or "exp" in name) else 0
    return (unstable, -version, 1 if (m and m.group(2)) else 0)


class OfflineBrain:
    available = False
    label = "Offline rules"

    def respond(self, *a, **k) -> Reply:  # pragma: no cover - never called
        raise BrainError("offline")


# =============================================================================
class GeminiBrain:
    def __init__(self, api_key: str, model: str = ""):
        self.api_key = api_key
        chain = [m.strip() for m in model.split(",") if m.strip()]
        for m in GEMINI_DEFAULT_MODELS:
            if m not in chain:
                chain.append(m)
        self.models, self.idx = chain, 0
        self.session = requests.Session()
        self.history: List[dict] = []
        self.streaming = os.environ.get("ALPHA_STREAMING", "1") != "0"
        self.thinking = os.environ.get("GEMINI_THINKING_LEVEL", "").strip()  # optional, e.g. "low"
        self._bearer = False
        self._discovered = False
        self._disabled = False

    @property
    def available(self) -> bool:
        return bool(self.api_key) and not self._disabled

    @property
    def label(self) -> str:
        return f"Gemini \u00b7 {self.models[self.idx]}"

    # ---------------------------------------------------------------- HTTP
    def _headers(self) -> dict:
        h = {"content-type": "application/json"}
        h["Authorization" if self._bearer else "x-goog-api-key"] = f"Bearer {self.api_key}" if self._bearer else self.api_key
        return h

    @staticmethod
    def _error_message(resp: requests.Response) -> str:
        try:
            return str(resp.json().get("error", {}).get("message", resp.text))[:240]
        except ValueError:
            return resp.text[:240]

    def _discover_models(self) -> None:
        self._discovered = True
        try:
            resp = self.session.get(f"{GEMINI_ROOT}/models", params={"pageSize": 200},
                                    headers=self._headers(), timeout=(5, 20))
            if resp.status_code != 200:
                return
            found = []
            for m in resp.json().get("models", []):
                name = m.get("name", "").replace("models/", "")
                if (name.startswith("gemini-") and "flash" in name
                        and "generateContent" in m.get("supportedGenerationMethods", [])
                        and not any(x in name for x in ("image", "tts", "live", "audio", "embedding",
                                                        "robotics", "computer", "thinking", "customtools"))):
                    found.append(name)
            new = [n for n in sorted(found, key=_gemini_model_rank) if n not in self.models]
            if new:
                log.info("Discovered additional Gemini models: %s", new[:5])
                self.models.extend(new[:5])
        except requests.RequestException:
            log.debug("Model discovery failed", exc_info=True)

    def _request(self, payload: dict, stream: bool) -> requests.Response:
        """POST with model rotation on 404/429, Bearer retry for AQ. keys, and backoff on 5xx."""
        last, rotations = "unknown error", 0
        for attempt in range(1, len(self.models) * 2 + 6):
            model = self.models[self.idx]
            method = "streamGenerateContent?alt=sse" if stream else "generateContent"
            try:
                resp = self.session.post(f"{GEMINI_ROOT}/models/{model}:{method}", headers=self._headers(),
                                         json=payload, timeout=(5, 60), stream=stream)
            except requests.RequestException as exc:
                last = str(exc)
                time.sleep(min(1.0 * attempt, 4))
                continue
            if resp.status_code == 200:
                return resp
            code, msg = resp.status_code, self._error_message(resp)
            resp.close()
            last = f"HTTP {code}: {msg}"
            if code in (401, 403):
                if self.api_key.startswith("AQ.") and not self._bearer:
                    self._bearer = True
                    log.info("Retrying Gemini request with Bearer authentication.")
                    continue
                self._disabled = True
                raise BrainError(f"Gemini rejected the API key ({msg})")
            if code in (404, 429):
                log.warning("Gemini model '%s' unavailable (%s); trying the next one.", model, code)
                if code == 404 and not self._discovered and self.idx == len(self.models) - 1:
                    self._discover_models()
                rotations += 1
                if rotations > len(self.models):
                    raise BrainError(last)
                self.idx = (self.idx + 1) % len(self.models)
                if code == 429:
                    time.sleep(0.4)
                continue
            if code in (500, 502, 503, 504):
                time.sleep(min(1.0 * attempt, 5))
                continue
            raise BrainError(last)
        raise BrainError(last)

    # ----------------------------------------------------------- streaming
    @staticmethod
    def _consume_stream(resp: requests.Response, on_text, on_first_token) -> tuple:
        """Parse SSE chunks. Parts are collected verbatim (thought signatures must be echoed back)."""
        resp.encoding = "utf-8"  # text/event-stream has no charset; requests would guess latin-1 and garble Urdu
        parts: List[dict] = []
        finish, first, blocked = None, False, False
        try:
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if not body or body == "[DONE]":
                    continue
                try:
                    chunk = json.loads(body)
                except ValueError:
                    continue
                if chunk.get("error"):
                    raise BrainError(f"Gemini stream error: {str(chunk['error'])[:200]}")
                cands = chunk.get("candidates") or []
                if not cands:
                    blocked = blocked or bool((chunk.get("promptFeedback") or {}).get("blockReason"))
                    continue
                for part in (cands[0].get("content") or {}).get("parts") or []:
                    parts.append(part)
                    text = part.get("text")
                    if text and not part.get("thought"):
                        if not first:
                            first = True
                            if on_first_token:
                                on_first_token()
                        if on_text:
                            on_text(text)
                finish = cands[0].get("finishReason") or finish
        except requests.RequestException as exc:
            raise BrainError(f"Gemini stream interrupted: {exc}")
        finally:
            resp.close()
        return parts, ("SAFETY" if blocked and not parts else finish)

    # ---------------------------------------------------------- conversation
    def respond(self, user_text: str, tools: List[dict], run_tool: RunTool, system: str,
                on_text: Optional[Callable[[str], None]] = None,
                on_first_token: Optional[Callable[[], None]] = None) -> Reply:
        decls = gemini_declarations(tools)
        stream = on_text is not None and self.streaming
        contents = self.history + [{"role": "user", "parts": [{"text": user_text}]}]
        final, spoken = "", False
        for _ in range(5):  # max tool-use rounds
            payload = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": contents,
                "tools": [{"functionDeclarations": decls}] if decls else [],
                "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
                "generationConfig": {"maxOutputTokens": 1024, "temperature": 0.4},
            }
            if not decls:
                payload.pop("tools"), payload.pop("toolConfig")
            if self.thinking:
                payload["generationConfig"]["thinkingConfig"] = {"thinkingLevel": self.thinking}
            if stream:
                parts, finish = self._consume_stream(self._request(payload, True), on_text, on_first_token)
            else:
                data = self._request(payload, False).json()
                cands = data.get("candidates") or []
                parts = (cands[0].get("content") or {}).get("parts") or [] if cands else []
                finish = cands[0].get("finishReason") if cands else "SAFETY"
            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            text = "".join(p["text"] for p in parts if p.get("text") and not p.get("thought")).strip()
            if text and stream:
                spoken = True
            if not calls:
                if finish == "SAFETY" and not text:
                    final = "Sorry, I can't help with that."
                elif not text:
                    final = "Done." if finish in (None, "STOP") else "Sorry, I couldn't finish that answer."
                else:
                    final = text
                if not stream and on_text and text:
                    on_text(text)
                    spoken = True
                break
            contents.append({"role": "model", "parts": parts})  # echoed unchanged (keeps thought signatures)
            replies = []
            for fc in calls:
                result = run_tool(fc.get("name", ""), fc.get("args") or {}) or "Done."
                fr = {"name": fc.get("name", ""), "response": {"result": result}}
                if fc.get("id"):
                    fr["id"] = fc["id"]
                replies.append({"functionResponse": fr})
            contents.append({"role": "user", "parts": replies})
        else:
            final = "Sorry, that took too many steps."
        final = final or "Done."
        self.history += [{"role": "user", "parts": [{"text": user_text}]},
                         {"role": "model", "parts": [{"text": final}]}]
        self.history = self.history[-12:]
        return Reply(final, spoken and bool(final))


# =============================================================================
class ClaudeBrain:
    def __init__(self, api_key: str, model: str = DEFAULT_CLAUDE_MODEL):
        self.api_key, self.model = api_key, model
        self.session = requests.Session()
        self.history: List[dict] = []

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def label(self) -> str:
        return f"Claude \u00b7 {self.model}"

    def _post(self, payload: dict) -> dict:
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        last: object = None
        for attempt in range(3):
            try:
                resp = self.session.post(CLAUDE_URL, headers=headers, json=payload, timeout=(5, 45))
            except requests.RequestException as exc:
                last = exc
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 529):
                last = f"HTTP {resp.status_code}"
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code in (401, 403):
                self.api_key = ""
            raise BrainError(f"API error {resp.status_code}: {resp.text[:200]}")
        raise BrainError(f"API unreachable: {last}")

    def respond(self, user_text: str, tools: List[dict], run_tool: RunTool, system: str,
                on_text=None, on_first_token=None) -> Reply:
        messages = self.history + [{"role": "user", "content": user_text}]
        final = ""
        for _ in range(5):
            data = self._post({"model": self.model, "max_tokens": 400, "system": system,
                               "tools": tools, "messages": messages})
            content = data.get("content", [])
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            if data.get("stop_reason") != "tool_use" or not tool_uses:
                final = " ".join(t for t in texts if t).strip()
                break
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tu["id"],
                 "content": run_tool(tu["name"], tu.get("input") or {}) or "Done."} for tu in tool_uses]})
        else:
            final = "Sorry, that took too many steps."
        final = final or "Done."
        self.history += [{"role": "user", "content": user_text}, {"role": "assistant", "content": final}]
        self.history = self.history[-12:]
        if on_first_token:
            on_first_token()
        if on_text:
            on_text(final)
        return Reply(final, bool(on_text))


def build_brain():
    """ALPHA_PROVIDER = auto (default) | gemini | claude. Auto prefers the free Gemini key."""
    provider = os.environ.get("ALPHA_PROVIDER", "auto").strip().lower()
    gemini_key = clean_key(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    claude_key = clean_key(os.environ.get("ANTHROPIC_API_KEY"))
    if provider in ("auto", "gemini") and gemini_key:
        return GeminiBrain(gemini_key, os.environ.get("GEMINI_MODEL", ""))
    if provider in ("auto", "claude", "anthropic") and claude_key:
        return ClaudeBrain(claude_key, os.environ.get("ALPHA_MODEL", DEFAULT_CLAUDE_MODEL))
    return OfflineBrain()
