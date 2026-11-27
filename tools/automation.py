"""tools/automation.py - Automation agent: run the user's n8n workflows through webhooks."""
from __future__ import annotations

import json

from core.registry import tool

A = "Automation (n8n)"
_ON = lambda svc: bool(getattr(svc, "n8n", None) and svc.n8n.enabled)  # noqa: E731


@tool("run_workflow", "Trigger one of the user's configured n8n workflows (email drafting, scraping, CRM...). "
      "Only configured workflow names work.",
      params={"workflow": {"type": "string", "description": "Workflow name"},
              "instruction": {"type": "string", "description": "What the workflow should do, in plain words"},
              "data_json": {"type": "string", "description": "Optional extra fields as a JSON object string"},
              "wait": {"type": "boolean", "description": "Wait for the answer (default true)"}},
      required=["workflow"], agent=A, available=_ON,
      confirm=lambda a, s: s.n8n.requires_confirmation(a.get("workflow", "")),
      confirm_text=lambda a: f"Run the n8n workflow {a.get('workflow', '')}? Please say yes or no.",
      title=lambda a: f"n8n workflow: {a.get('workflow', '')}")
def run_workflow(ctx, workflow: str, instruction: str = "", data_json=None, wait=True) -> str:
    n8n = ctx.svc.n8n
    data: dict = {}
    if data_json:
        try:
            parsed = json.loads(data_json) if isinstance(data_json, str) else data_json
            data = parsed if isinstance(parsed, dict) else {"value": parsed}
        except ValueError:
            data = {"text": str(data_json)}
    ctx.step("Building the JSON payload")
    if wait is False:
        ctx.step("Queueing the webhook in the background")
        n8n.trigger_async(workflow, instruction, data)
        return "Started in the background. I'll tell you when n8n answers."
    ctx.step(f"Calling the n8n webhook ({workflow})")
    return n8n.trigger(workflow, instruction, data).speakable()


@tool("list_workflows", "List the n8n workflows that are available.", agent=A, available=_ON, fast=True,
      title=lambda a: "Listing n8n workflows")
def list_workflows(ctx) -> str:
    return "Available workflows: " + ctx.svc.n8n.describe() + "."
