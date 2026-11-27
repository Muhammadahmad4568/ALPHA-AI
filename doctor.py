"""
scripts/doctor.py - `python main.py doctor`: checks everything Alpha needs and tells you exactly how to fix what is missing.
"""
from __future__ import annotations

import importlib
import os
import platform
import socket
import sys
from pathlib import Path
from typing import List, Tuple

from config.settings import CONTACTS_FILE, DATA_DIR, ROOT, VOICE_PROFILE, env_float

OK, WARN, BAD = "[ OK ]", "[WARN]", "[FAIL]"
STRUCTURE = ("config", "core", "ui", "tools", "voice", "voice_security", "scripts")
MODULES = ["config.settings", "core.events", "core.registry", "core.brains", "core.orchestrator", "core.assistant",
           "core.memory", "core.instance", "ui.asgi", "ui.server", "voice.engine", "voice_security.voice_auth",
           "tools.pc_controller", "tools.whatsapp_tool", "tools.web_reader", "tools.n8n_client"]
PACKAGES = [("uvicorn", True), ("requests", True), ("numpy", True), ("psutil", True), ("faster_whisper", True),
            ("edge_tts", True), ("pygame", True), ("sounddevice", True), ("send2trash", True), ("pyautogui", False),
            ("pygetwindow", False), ("bs4", True), ("lxml", False), ("playwright", True), ("speech_recognition", False),
            ("pycaw", False)]


def run() -> int:
    results: List[Tuple[str, str, str]] = []
    add = lambda status, what, hint="": results.append((status, what, hint))  # noqa: E731

    v = sys.version_info
    add(OK if (3, 10) <= v[:2] <= (3, 13) else WARN, f"Python {v.major}.{v.minor}.{v.micro} ({platform.system()} {platform.machine()})",
        "" if (3, 10) <= v[:2] <= (3, 13) else "Python 3.10-3.12 (64-bit) is recommended")

    missing = [d for d in STRUCTURE if not (ROOT / d / "__init__.py").is_file()]
    add(BAD if missing else OK, f"Project root: {ROOT}" + (f"  (missing: {', '.join(missing)})" if missing else ""),
        "main.py and the folders config, core, ui, tools, voice, voice_security must sit side by side in ONE folder" if missing else "")
    nested = ROOT / ROOT.name
    if nested.is_dir() and (nested / "main.py").is_file():
        add(WARN, f"A second copy of the project exists at {nested}", "You extracted the zip inside the project folder: delete the inner copy")

    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:
            add(BAD, f"import {name}", f"{type(exc).__name__}: {exc}")
    if not any(r[0] == BAD and r[1].startswith("import") for r in results):
        add(OK, f"all {len(MODULES)} Alpha modules import cleanly")

    for pkg, required in PACKAGES:
        try:
            m = importlib.import_module(pkg)
            add(OK, f"package {pkg} {getattr(m, '__version__', '')}".strip())
        except Exception as exc:
            add(BAD if required else WARN, f"package {pkg} not usable ({type(exc).__name__})",
                "pip install -r requirements.txt" + ("" if required else "  (optional)"))

    env = ROOT / ".env"
    if not env.exists():
        add(WARN, ".env not found", "copy .env.example .env  and put your free Gemini key in it")
    else:
        key = os.environ.get("GEMINI_API_KEY", "")
        has_key = bool(key) and not key.lower().startswith(("paste", "your"))
        add(OK if has_key or os.environ.get("ANTHROPIC_API_KEY") else WARN, ".env found; " + ("AI key set" if has_key else "no AI key yet"),
            "" if has_key else "get a free key at https://aistudio.google.com/apikey and set GEMINI_API_KEY")
    try:
        probe = DATA_DIR / ".write_test"
        probe.write_text("x")
        probe.unlink()
        add(OK, f"data folder writable: {DATA_DIR}")
    except OSError as exc:
        add(BAD, f"data folder not writable: {DATA_DIR}", str(exc))

    port = int(env_float("ALPHA_WEB_PORT", 8765))
    from core.instance import probe_port
    running = probe_port("127.0.0.1", port)
    if running:
        add(WARN, f"Alpha is ALREADY running on port {port} (process {running.get('pid')})",
            "close it first (Ctrl+C in its window, or end python.exe in Task Manager); two copies fight over the mic")
    else:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                add(OK, f"dashboard port {port} is free")
            except OSError:
                add(WARN, f"port {port} is used by another program", "Alpha will pick the next free port automatically")

    try:
        import sounddevice as sd

        dev = sd.query_devices(None, "input")
        add(OK, f"default microphone: {dev['name']}")
    except Exception as exc:
        add(BAD, f"no usable microphone ({type(exc).__name__}: {str(exc)[:70]})",
            "check Windows Settings > Privacy & security > Microphone, plug in a mic, or run with --text")

    from ui.server import _find_chromium_browser
    browser = _find_chromium_browser()
    add(OK if browser else WARN, f"browser for the app window / WhatsApp: {browser or 'not found'}",
        "" if browser else "install Microsoft Edge or Chrome (or: python -m playwright install chromium)")

    add(OK if VOICE_PROFILE.exists() else WARN, "voice profile " + ("found" if VOICE_PROFILE.exists() else "not enrolled yet"),
        "" if VOICE_PROFILE.exists() else "python main.py enroll   (voice commands are refused until you do; typing still works)")
    add(OK if CONTACTS_FILE.exists() else WARN, "WhatsApp contacts " + ("found" if CONTACTS_FILE.exists() else "not set up"),
        "" if CONTACTS_FILE.exists() else "copy config\\contacts.example.json config\\contacts.json   (only needed for WhatsApp)")

    print("\nAlpha AI doctor\n" + "=" * 60)
    for status, what, hint in results:
        print(f"{status} {what}")
        if hint:
            print(f"       -> {hint}")
    bad = sum(1 for r in results if r[0] == BAD)
    warn = sum(1 for r in results if r[0] == WARN)
    print("=" * 60 + f"\n{bad} problem(s), {warn} warning(s)." + ("  Fix the [FAIL] lines first." if bad else "  Ready to run: python main.py"))
    return 1 if bad else 0
