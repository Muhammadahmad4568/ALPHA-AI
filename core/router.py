"""
core/router.py - rule-based intents.

`match()` is the full offline router. `fast()` is the safe subset: only tools flagged fast=True in the
registry, only short commands. Everything else goes to the LLM, which sees the same tools.
"""
from __future__ import annotations

import re
from typing import Any, Optional, Tuple

from core.registry import ToolRegistry

Intent = Tuple[str, dict]
_URL = re.compile(r"(https?://\S+|(?:www\.)?[\w-]+(?:\.[\w-]+)+/\S*|[\w-]+\.(?:com|org|net|io|pk|co|edu|gov)\b\S*)", re.I)


class RuleRouter:
    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    def match(self, text: str) -> Optional[Intent]:
        t = re.sub(r"^(please|can you|could you|alpha)[, ]+", "", text.strip().rstrip(".!?"), flags=re.I)
        low = t.lower()
        period = "yesterday" if "yesterday" in low else "week" if "week" in low else "today"

        # --- custom tools: regexes declared with @tool(triggers=[...]); named groups become the arguments
        for spec in self.registry.all():
            for pattern in spec.triggers:
                try:
                    m = re.search(pattern, t, re.I)
                except re.error:
                    continue
                if m:
                    return spec.name, {k: v for k, v in m.groupdict().items() if v is not None}

        # --- WhatsApp (always confirmed by the tool before anything is sent)
        m = (re.match(r"(?:send|write)\s+(?:a\s+)?(?:whatsapp\s+)?(?:message\s+)?(?:on\s+whatsapp\s+)?to\s+(.+?)\s+"
                      r"(?:on\s+whatsapp\s+)?(?:saying|say|that|with|:)\s+(.+)$", t, re.I)
             or re.match(r"(?:whatsapp|message|text)\s+(.+?)\s+(?:saying|that|:)\s+(.+)$", t, re.I))
        if m:
            return "send_whatsapp", {"contact": m.group(1).strip(), "message": m.group(2).strip()}

        # --- web
        m = re.match(r"(?:summari[sz]e|read|open and read)\s+(?:this\s+|the\s+)?(?:page\s+|article\s+|link\s+|url\s+)?(.+)$", t, re.I)
        if m and _URL.search(m.group(1)):
            return "read_webpage", {"url": _URL.search(m.group(1)).group(1)}
        m = re.match(r"(?:search|google|look up|find)\s+(?:the\s+)?(?:web|internet|online)\s+(?:for\s+)?(.+)$", t, re.I) \
            or re.match(r"(?:google|look up)\s+(.+)$", t, re.I)
        if m:
            return "web_search", {"query": m.group(1).strip()}
        m = re.match(r"(?:what(?:'s| is| are)\s+the\s+)?(?:latest|newest|recent)\s+(?:news|updates?)\s+(?:on|about|for)\s+(.+)$", t, re.I)
        if m:
            return "web_updates", {"topic": m.group(1).strip()}

        # --- system
        if re.search(r"\bcancel (the )?(shutdown|restart)\b", low):
            return "system_action", {"action": "cancel_shutdown"}
        if re.search(r"\b(shut ?down|power off|turn off)\b.*\b(pc|computer|laptop|system)\b|^shut ?down\b", low):
            return "system_action", {"action": "shutdown"}
        if re.search(r"\b(restart|reboot)\b", low):
            return "system_action", {"action": "restart"}
        if re.search(r"^(go to )?sleep\b|\b(put|pc|computer|laptop)\b.*\bsleep\b", low):
            return "system_action", {"action": "sleep"}
        if re.search(r"^lock\b|\block\b.*\b(pc|computer|screen|laptop)\b", low):
            return "system_action", {"action": "lock"}
        if "unmute" in low:
            return "audio_control", {"action": "unmute"}
        if re.search(r"\bmute\b", low):
            return "audio_control", {"action": "mute"}
        m = re.search(r"\bvolume\b\D{0,12}(\d{1,3})", low)
        if m:
            return "audio_control", {"action": "set_volume", "level": int(m.group(1))}
        if re.search(r"volume up|(increase|raise|turn up)\b.*\bvolume|\blouder\b", low):
            return "audio_control", {"action": "volume_up"}
        if re.search(r"volume down|(decrease|lower|reduce|turn down)\b.*\bvolume|\bquieter\b", low):
            return "audio_control", {"action": "volume_down"}
        if re.search(r"productiv|focus score", low):
            return "productivity_report", {"period": period}
        if re.search(r"screen ?time|how long (have i|did i) (been )?(use|used|on)", low):
            return "screen_time", {"period": period}
        if re.search(r"^(show|go to) (the )?desktop$|^minimi[sz]e all", low):
            return "window_control", {"action": "minimize_all"}
        if re.search(r"^(list|what) (my )?(open )?windows|^which windows", low):
            return "window_control", {"action": "list"}
        m = re.match(r"(minimi[sz]e|maximi[sz]e|restore|focus|switch to|close)\s+(?:the\s+)?(.+?)(?:\s+window)?$", t, re.I)
        if m and not re.match(r"(all|everything)$", m.group(2), re.I):
            verb = m.group(1).lower()
            return "window_control", {"action": {"minimise": "minimize", "maximise": "maximize",
                                                 "switch to": "focus"}.get(verb, verb), "title": m.group(2)}
        if re.match(r"(?:list|show)\s+(?:my\s+)?workflows", low):
            return "list_workflows", {}
        m = re.match(r"(?:run|trigger|start)\s+(?:the\s+)?(.+?)\s+workflow(?:\s+(?:to|for|with)\s+(.+))?$", t, re.I)
        if m:
            return "run_workflow", {"workflow": m.group(1), "instruction": m.group(2) or ""}
        m = re.match(r"(?:remember|save)\s+(?:my\s+)?(.+?)\s+project\s+(?:is\s+)?(?:at|in)\s+(.+)", t, re.I)
        if m:
            return "save_project", {"name": m.group(1), "path": m.group(2)}
        if re.match(r"(?:list|show)\s+(?:my\s+)?projects", low):
            return "list_projects", {}
        m = re.match(r"(?:where(?:'s| is| are)|locate)\s+(?:my\s+|the\s+)?(.+)", t, re.I)
        if m:
            return "find_project", {"name": m.group(1)}
        m = re.match(r"(?:find|search for|search)\s+(?:the\s+)?(?:file\s+|folder\s+)?(.+)", t, re.I)
        if m:
            return "search_files", {"query": m.group(1)}
        m = re.match(r"(?:take a note|note down|note that|remember that)\s*[:,]?\s*(.+)", t, re.I)
        if m:
            return "add_note", {"text": m.group(1)}
        if re.match(r"(?:read|show)\s+(?:me\s+)?(?:my\s+)?notes", low):
            return "get_notes", {}
        m = re.match(r"create\s+(?:a\s+)?(?:new\s+)?folder\s+(?:named\s+|called\s+)?(.+)", t, re.I)
        if m:
            return "create_folder", {"path": m.group(1)}
        m = re.match(r"create\s+(?:a\s+)?(?:new\s+)?file\s+(?:named\s+|called\s+)?(.+)", t, re.I)
        if m:
            return "create_file", {"path": m.group(1)}
        m = re.match(r"delete\s+(?:the\s+)?(?:file\s+|folder\s+)?(.+)", t, re.I)
        if m:
            return "delete_path", {"path": m.group(1)}
        m = re.match(r"(?:open|launch|start|run)\s+(?:the\s+)?(.+)", t, re.I)
        if m:
            return "open_app", {"name": m.group(1)}
        return None

    def fast(self, text: str, svc: Any = None) -> Optional[Intent]:
        """Short commands whose tool is registered as fast (safe, local) skip the LLM."""
        if len(text.split()) > 9:
            return None
        intent = self.match(text)
        if not intent:
            return None
        spec = self.registry.get(intent[0])
        if spec and spec.is_available(svc) and spec.is_fast(intent[1]):
            return intent
        return None
