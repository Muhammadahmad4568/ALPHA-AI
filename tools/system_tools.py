"""tools/system_tools.py - System Control agent: power, volume, apps, windows, files."""
from __future__ import annotations

from pathlib import Path

from core.registry import tool

S = {"type": "string"}


@tool("system_action", "Power/session control: shutdown, restart, sleep, lock, cancel_shutdown. "
      "Shutdown and restart ask the user for confirmation automatically.",
      params={"action": {"type": "string", "enum": ["shutdown", "restart", "sleep", "lock", "cancel_shutdown"]}},
      required=["action"], agent="System Control",
      confirm=lambda a, s: a.get("action") in ("shutdown", "restart"),
      confirm_text=lambda a: f"Are you sure you want to {'shut down' if a.get('action') == 'shutdown' else 'restart'} "
                             "the computer? Please say yes or no.",
      fast=lambda a: a.get("action") in ("lock", "sleep", "cancel_shutdown"),
      title=lambda a: {"shutdown": "Shutting down", "restart": "Restarting", "sleep": "Going to sleep",
                       "lock": "Locking the PC", "cancel_shutdown": "Cancelling shutdown"}.get(a.get("action"), "System action"))
def system_action(ctx, action: str) -> str:
    ctx.step("Sending the command to Windows")
    return ctx.svc.pc.system_action(action, confirmed=ctx.confirmed)


@tool("audio_control", "Mute, unmute or change the system volume.",
      params={"action": {"type": "string", "enum": ["mute", "unmute", "toggle_mute", "volume_up", "volume_down", "set_volume"]},
              "level": {"type": "integer", "minimum": 0, "maximum": 100, "description": "For set_volume"}},
      required=["action"], fast=True, title=lambda a: "Audio: " + str(a.get("action", "")).replace("_", " "))
def audio_control(ctx, action: str, level=None) -> str:
    ctx.step("Changing the audio setting")
    return ctx.svc.pc.audio(action, level)


@tool("open_app", "Open an application by spoken name: on-screen keyboard, task manager, notepad, command prompt, "
      "chrome, word, spotify... Falls back to administrator elevation (UAC) when Windows requires it.",
      params={"name": S}, required=["name"], fast=True, title=lambda a: f"Opening {a.get('name', 'app')}")
def open_app(ctx, name: str) -> str:
    ctx.step(f"Looking for {name}")
    ctx.step("Launching it")
    return ctx.svc.pc.open_app(name)


@tool("window_control", "Manage windows: list, focus, minimize, maximize, restore, close (needs confirmation), "
      "or minimize_all to show the desktop.",
      params={"action": {"type": "string", "enum": ["list", "focus", "minimize", "maximize", "restore", "close", "minimize_all"]},
              "title": {"type": "string", "description": "Part of the window title"}},
      required=["action"], confirm=lambda a, s: a.get("action") == "close",
      confirm_text=lambda a: f"Close the window {a.get('title', '')}? Please say yes or no.",
      fast=lambda a: a.get("action") != "close", title=lambda a: f"Window: {a.get('action', '')} {a.get('title', '')}".strip())
def window_control(ctx, action: str, title: str = "") -> str:
    ctx.step("Finding the window")
    return ctx.svc.pc.window_control(action, title, confirmed=ctx.confirmed)


@tool("create_folder", "Create a folder. Relative paths go on the Desktop.", params={"path": S}, required=["path"],
      title=lambda a: f"Creating folder {a.get('path', '')}")
def create_folder(ctx, path: str) -> str:
    return ctx.svc.pc.create_folder(path)


@tool("create_file", "Create a new text file (never overwrites an existing one).",
      params={"path": S, "content": S}, required=["path"], title=lambda a: f"Creating file {a.get('path', '')}")
def create_file(ctx, path: str, content: str = "") -> str:
    return ctx.svc.pc.create_file(path, content)


@tool("move_path", "Move a file or folder.", params={"source": S, "destination": S}, required=["source", "destination"],
      title=lambda a: f"Moving {a.get('source', '')}")
def move_path(ctx, source: str, destination: str) -> str:
    return ctx.svc.pc.move_path(source, destination)


@tool("delete_path", "Delete a file or folder (it goes to the Recycle Bin). Confirmation is automatic.",
      params={"path": S}, required=["path"], confirm=True,
      confirm_text=lambda a: f"Are you sure you want to delete {Path(str(a.get('path', ''))).name or a.get('path', '')}? "
                             "Please say yes or no.",
      title=lambda a: f"Deleting {a.get('path', '')}")
def delete_path(ctx, path: str) -> str:
    ctx.step("Checking the path is safe to delete")
    return ctx.svc.pc.delete_path(path, confirmed=ctx.confirmed)


@tool("search_files", "Search the PC for files or folders by name.", params={"query": S}, required=["query"],
      fast=True, title=lambda a: f"Searching for {a.get('query', '')}")
def search_files(ctx, query: str) -> str:
    ctx.step("Searching your drives")
    return ctx.svc.pc.search_files_text(query)
