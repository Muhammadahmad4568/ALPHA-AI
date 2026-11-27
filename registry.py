r"""
core/registry.py - the extensible tool registry.

Adding a tool = writing one function with a decorator (anywhere under tools/ or tools/custom/):

    from core.registry import tool

    @tool("coin_flip", "Flip a coin.", agent="Fun")
    def coin_flip(ctx) -> str:
        ctx.step("Flipping")
        import random
        return random.choice(["Heads", "Tails"])

    @tool("greet", "Greet someone.", params={"name": {"type": "string"}}, required=["name"])
    def greet(ctx, name: str) -> str:
        return f"Hello {name}!"

Every module in `tools/` and `tools/custom/` is imported at startup, so the new tool is available to
the voice/LLM pipeline and the step-by-step execution panel with no other change.

fn(ctx, **args) -> str receives a TaskContext:
    ctx.step("text")        add a step to the execution panel (the previous step is marked done)
    ctx.svc.<service>       pc, memory, tracker, n8n, whatsapp, web ... (see core/assistant.py)
    ctx.confirmed           True after the user said yes to a `confirm=True` tool
Raise an exception, or return a message, to report a failure; the panel shows it.

triggers=[...]  optional regexes that let plain voice/text commands reach your tool without the LLM; named groups become
                the arguments, e.g.  triggers=[r"convert (?P<value>[\d.]+) degrees to (?P<to>celsius|fahrenheit)"]
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

log = logging.getLogger("alpha.registry")


class TaskContext:
    """Passed to every tool; turns what the tool does into the step-by-step panel."""

    def __init__(self, task_id: str, agent: str, tool: str, title: str, svc: Any,
                 emit: Optional[Callable[[dict], None]] = None):
        self.id, self.agent, self.tool, self.title, self.svc, self._emit = task_id, agent, tool, title, svc, emit
        self.steps: List[dict] = []
        self.status, self.detail, self.confirmed = "running", "", False
        self.started = time.perf_counter()

    def step(self, text: str, status: str = "running") -> int:
        for s in self.steps:  # a new step completes the one before it
            if s["status"] == "running":
                s["status"] = "done"
        self.steps.append({"i": len(self.steps) + 1, "text": text, "status": status})
        self._push()
        return len(self.steps)

    def mark(self, index: int, status: str, text: Optional[str] = None) -> None:
        if 1 <= index <= len(self.steps):
            self.steps[index - 1]["status"] = status
            if text:
                self.steps[index - 1]["text"] = text
            self._push()

    def finish(self, status: str, detail: str = "") -> None:
        for s in self.steps:
            if s["status"] == "running":
                s["status"] = "failed" if status == "failed" else ("skipped" if status == "declined" else "done")
        self.status, self.detail = status, detail
        self._push()

    def _push(self) -> None:
        if self._emit:
            self._emit({"id": self.id, "agent": self.agent, "tool": self.tool, "title": self.title,
                        "status": self.status, "steps": [dict(s) for s in self.steps], "detail": self.detail[:240],
                        "ms": int((time.perf_counter() - self.started) * 1000), "updated": time.time()})


@dataclass
class ToolSpec:
    name: str
    description: str
    fn: Callable[..., str]
    properties: Dict[str, dict]
    required: List[str]
    agent: str
    confirm: Union[bool, Callable[[dict, Any], bool]] = False   # callable(args, services) -> bool
    confirm_text: Optional[Callable[[dict], str]] = None
    fast: Union[bool, Callable[[dict], bool]] = False           # safe to run straight from rules (no LLM)
    title: Optional[Callable[[dict], str]] = None
    available: Optional[Callable[[Any], bool]] = None           # hide the tool until e.g. n8n is configured
    triggers: List[str] = field(default_factory=list)           # regexes the rule router (voice fast path) understands
    module: str = ""

    def needs_confirmation(self, args: dict, svc: Any = None) -> bool:
        return bool(self.confirm(args, svc)) if callable(self.confirm) else bool(self.confirm)

    def is_fast(self, args: dict) -> bool:
        return bool(self.fast(args)) if callable(self.fast) else bool(self.fast)

    def is_available(self, svc: Any) -> bool:
        try:
            return True if self.available is None else bool(self.available(svc))
        except Exception:
            return False

    def title_for(self, args: dict) -> str:
        try:
            return self.title(args) if self.title else self.name.replace("_", " ").capitalize()
        except Exception:
            return self.name.replace("_", " ").capitalize()

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": {"type": "object", "properties": self.properties, "required": self.required}}


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, ToolSpec] = {}
        self.load_errors: List[str] = []

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            log.warning("Tool '%s' redefined by %s (was %s)", spec.name, spec.module, self._tools[spec.name].module)
        self._tools[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def all(self) -> List[ToolSpec]:
        return list(self._tools.values())

    def schemas(self, svc: Any = None) -> List[dict]:
        return [t.schema() for t in self._tools.values() if t.is_available(svc)]

    def agents(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for t in self._tools.values():
            out.setdefault(t.agent, []).append(t.name)
        return out

    def discover(self, packages=("tools", "tools.custom")) -> None:
        """Import every module of the given packages so their @tool decorators run."""
        for pkg_name in packages:
            try:
                pkg = importlib.import_module(pkg_name)
            except Exception as exc:
                self.load_errors.append(f"{pkg_name}: {exc}")
                log.error("Could not import package %s: %s", pkg_name, exc)
                continue
            for mod in pkgutil.iter_modules(pkg.__path__):
                if mod.name.startswith("_") or mod.ispkg:
                    continue
                full = f"{pkg_name}.{mod.name}"
                try:
                    importlib.import_module(full)
                except Exception as exc:  # a broken custom tool must never stop Alpha
                    self.load_errors.append(f"{full}: {exc}")
                    log.error("Tool module %s failed to load (skipped): %s", full, exc)
                    log.debug("traceback", exc_info=True)


REGISTRY = ToolRegistry()


def tool(name: str, description: str, params: Optional[Dict[str, dict]] = None, required: Optional[List[str]] = None,
         agent: str = "System Control", confirm: Union[bool, Callable[[dict, Any], bool]] = False,
         confirm_text: Optional[Callable[[dict], str]] = None, fast: Union[bool, Callable[[dict], bool]] = False,
         title: Optional[Callable[[dict], str]] = None, available: Optional[Callable[[Any], bool]] = None,
         triggers: Optional[List[str]] = None):
    """Decorator that registers fn(ctx, **args) -> str as an AI-callable tool."""
    def deco(fn: Callable[..., str]) -> Callable[..., str]:
        REGISTRY.register(ToolSpec(name, description, fn, params or {}, required or [], agent, confirm,
                                   confirm_text, fast, title, available, list(triggers or []),
                                   getattr(fn, "__module__", "")))
        return fn
    return deco


class Services:
    """Plain attribute bag: pc, memory, tracker, n8n, whatsapp, web, ..."""

    def __init__(self, **kw: Any):
        self.__dict__.update(kw)

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"service '{name}' is not available")
