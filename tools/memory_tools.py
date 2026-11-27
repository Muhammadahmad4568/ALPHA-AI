"""tools/memory_tools.py - remembered project folders and notes (SQLite)."""
from __future__ import annotations

from core.registry import tool

S = {"type": "string"}
A = "Memory & Notes"


@tool("save_project", "Remember a project's folder so it can be found later.", params={"name": S, "path": S},
      required=["name", "path"], agent=A, title=lambda a: f"Saving project {a.get('name', '')}")
def save_project(ctx, name: str, path: str) -> str:
    return ctx.svc.pc.save_project(name, path)


@tool("find_project", "Look up where a remembered project lives ('where is my X project?'); searches the disk if unknown.",
      params={"name": S}, required=["name"], agent=A, fast=True, title=lambda a: f"Finding project {a.get('name', '')}")
def find_project(ctx, name: str) -> str:
    ctx.step("Checking remembered projects")
    return ctx.svc.pc.locate_project(name)


@tool("list_projects", "List all remembered projects.", agent=A, fast=True, title=lambda a: "Listing projects")
def list_projects(ctx) -> str:
    return ctx.svc.pc.list_projects()


@tool("add_note", "Save a note for the user.", params={"text": S}, required=["text"], agent=A, fast=True,
      title=lambda a: "Saving a note")
def add_note(ctx, text: str) -> str:
    return ctx.svc.pc.add_note(text)


@tool("get_notes", "Read back the user's latest notes.", agent=A, fast=True, title=lambda a: "Reading notes")
def get_notes(ctx) -> str:
    return ctx.svc.pc.get_notes()
