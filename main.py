#!/usr/bin/env python3
"""
Alpha AI v4 launcher.

    python main.py                       run Alpha + the dashboard           (same as: python main.py run)
    python main.py run --text            typed commands only, no microphone
    python main.py enroll                record your voice for the voice lock    [--samples 3-5] [--impostor]
    python main.py voice-test            live match scores for whoever speaks    [--seconds N]
    python main.py voice-status          show the voice profile
    python main.py voice-reset           delete the voice profile
    python main.py mic-test              12-second microphone level / recognition test
    python main.py list-mics             list audio input devices
    python main.py tools                 list every registered tool
    python main.py doctor                check Python, files, packages, mic, ports; prints how to fix problems
    python main.py migrate OLD_DIR       import settings, memory and custom tools from an older Alpha folder

Design rules this file follows (each one fixes a failure seen in earlier versions):
  1. bootstrap() runs BEFORE any project import: it pins the project root on sys.path and detects/repairs the
     "one folder too deep" extraction mistake, so `ModuleNotFoundError: config` cannot happen silently.
  2. ONE level of sub-commands. Each sub-command declares exactly the options it uses, and its handler reads only
     those, so a missing-attribute AttributeError is structurally impossible (tests/test_cli.py exercises all ten).
  3. Startup is verified (ui/server.py opens a real SSE connection to itself) before a window is shown.
  4. Every failure ends in a readable message and a non-zero exit code, never a bare traceback.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUIRED_DIRS = ("config", "core", "ui", "tools", "voice", "voice_security")


def _missing_dirs(root: Path) -> list:
    return [d for d in REQUIRED_DIRS if not (root / d / "__init__.py").is_file()]


def bootstrap() -> None:
    """Pin the project root and survive the nested-folder extraction trap. Must run before project imports."""
    missing = _missing_dirs(ROOT)
    if missing:
        nested = next((c for c in sorted(ROOT.iterdir())
                       if c.is_dir() and (c / "main.py").is_file() and not _missing_dirs(c)), None)
        if nested is not None and not os.environ.get("ALPHA_REEXEC"):
            print(f"[!] The project files are one folder too deep:\n      {nested}\n"
                  f"    Running from there for now. Please move everything inside it up into:\n      {ROOT}\n")
            env = dict(os.environ, ALPHA_REEXEC="1")
            sys.exit(subprocess.call([sys.executable, str(nested / "main.py"), *sys.argv[1:]], cwd=str(nested), env=env))
        print("[FAIL] Alpha cannot find its own folders next to main.py.\n"
              f"       main.py is in:   {ROOT}\n       missing there:   {', '.join(missing)}\n"
              "       Fix: main.py, config\\, core\\, ui\\, tools\\, voice\\, voice_security\\ must all sit side by side in ONE\n"
              "       folder (for example C:\\AlphaAIv4). Re-extract the zip so that `dir` shows main.py and those folders.")
        sys.exit(3)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)


bootstrap()

import argparse  # noqa: E402
import logging  # noqa: E402
import queue  # noqa: E402
import secrets  # noqa: E402
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler  # noqa: E402
from typing import Callable, List, Optional  # noqa: E402

import config.settings as cfg  # noqa: E402,F401  (importing it loads .env)
from config.settings import LOG_FILE, env_float, fixed_token  # noqa: E402

COMMANDS = ("run", "enroll", "voice-test", "voice-status", "voice-reset", "mic-test", "list-mics", "tools", "doctor", "migrate")


# =============================================================================
# helpers
# =============================================================================
def utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def setup_logging(verbose: bool, bus=None) -> QueueListener:
    """Logging goes through a queue (a slow disk never stalls the voice threads) and optionally to the UI console."""
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if verbose else logging.WARNING)
    ch.setFormatter(fmt)
    q: "queue.Queue" = queue.Queue(-1)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(QueueHandler(q))
    if bus is not None:
        from core.events import BusLogHandler

        root.addHandler(BusLogHandler(bus))
    for noisy in ("urllib3", "asyncio", "uvicorn.error", "watchfiles", "playwright"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    listener = QueueListener(q, fh, ch, respect_handler_level=True)
    listener.start()
    return listener


def samples_type(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' is not a number")
    if not 3 <= value <= 8:  # FIX: was capped at 5; voice_auth.PHRASES now has 8 entries
        raise argparse.ArgumentTypeError("choose between 3 and 8 phrases")
    return value


# =============================================================================
# command handlers: each reads ONLY the options its own sub-parser declares
# =============================================================================
def start_dashboard(args: argparse.Namespace, alpha, bus):
    """Start + self-verify the dashboard. Returns the handle, or None (Alpha then keeps running without a UI)."""
    try:
        from ui.server import open_dashboard, start_dashboard_server

        token = fixed_token() or secrets.token_urlsafe(16)
        web = start_dashboard_server(alpha, bus, args.host, args.port, token)
    except ImportError as exc:
        print(f"[!] Dashboard disabled - a package is missing: {exc}.  Run: pip install -r requirements.txt")
        return None
    except RuntimeError as exc:
        print(f"[!] Dashboard disabled - it failed its startup self-check:\n    {exc}\n"
              "    Alpha keeps running by voice/typing. Run `python main.py doctor` for details.")
        return None
    print(f"Dashboard: {web.url}  (startup self-check passed)")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("[!] The dashboard is reachable from your network. It controls this PC: only on a network you trust.")
    if not args.no_browser and os.environ.get("ALPHA_OPEN_BROWSER", "1") != "0":
        open_dashboard(web.url, app_window=(args.window == "app"))
    return web


def cmd_run(args: argparse.Namespace) -> int:
    from core.events import EventBus
    from core.instance import InstanceLock

    lock = InstanceLock()
    lock.acquire(args.host, args.port)                 # raises AlreadyRunning -> handled in main()
    bus = EventBus()
    listener = setup_logging(args.verbose, bus)
    alpha = web = None
    try:
        from core.assistant import Alpha

        alpha = Alpha(args, bus)
        if not args.no_ui:
            web = start_dashboard(args, alpha, bus)
        alpha.run()
    except KeyboardInterrupt:
        print("\nInterrupted - shutting down.")
    finally:
        for name, step in (("assistant", lambda: alpha and alpha.shutdown()), ("dashboard", lambda: web and web.stop()),
                           ("logging", listener.stop), ("lock", lock.release)):
            try:
                step()
            except Exception as exc:                  # shutting down must never raise
                print(f"(shutdown: {name}: {exc})")
    return 0


def cmd_enroll(args: argparse.Namespace) -> int:
    from voice_security import voice_auth

    return voice_auth.main(["enroll", "--samples", str(args.samples)] + (["--impostor"] if args.impostor else []))


def cmd_voice_test(args: argparse.Namespace) -> int:
    from voice_security import voice_auth

    return voice_auth.main(["test", "--seconds", str(args.seconds)])


def cmd_voice_status(args: argparse.Namespace) -> int:
    from voice_security import voice_auth

    return voice_auth.main(["status"])


def cmd_voice_reset(args: argparse.Namespace) -> int:
    from voice_security import voice_auth

    return voice_auth.main(["reset"])


def cmd_mic_test(args: argparse.Namespace) -> int:
    from voice.engine import run_mic_test

    run_mic_test()
    return 0


def cmd_list_mics(args: argparse.Namespace) -> int:
    import sounddevice as sd

    print(sd.query_devices())
    print("\nSet ALPHA_MIC=<index or part of the name> in .env to choose an input device.")
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    from core.registry import REGISTRY

    REGISTRY.discover()
    for agent, names in sorted(REGISTRY.agents().items()):
        print(f"\n{agent}")
        for n in names:
            t = REGISTRY.get(n)
            flags = ("confirm " if t.confirm else "") + ("fast " if t.fast else "") + ("(needs setup) " if t.available else "")
            print(f"  {n:22} {flags:22} {t.description[:70]}")
    if REGISTRY.load_errors:
        print("\nTool modules that failed to load (skipped):")
        for e in REGISTRY.load_errors:
            print("  -", e)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from scripts.doctor import run

    return run()


def cmd_migrate(args: argparse.Namespace) -> int:
    from scripts.migrate_old_project import migrate

    return migrate(args.old_dir)


# =============================================================================
# argument parsing
# =============================================================================
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", help="debug output (and full tracebacks on errors)")

    parser = argparse.ArgumentParser(prog="python main.py", description="Alpha AI v4 - voice assistant for Windows",
                                     epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", help="what to do (default: run)")

    def add(name: str, handler: Callable[[argparse.Namespace], int], help_text: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, parents=[common], help=help_text, description=help_text)
        sp.set_defaults(handler=handler)
        return sp

    run = add("run", cmd_run, "run Alpha and the dashboard (default)")
    run.add_argument("--text", action="store_true", help="no microphone; type commands (Alpha still speaks)")
    run.add_argument("--mute", action="store_true", help="start with spoken replies muted")
    run.add_argument("--whisper", default=None, metavar="MODEL", help="speech model: tiny | base | small")
    run.add_argument("--no-tracking", action="store_true", help="disable screen-time logging")
    run.add_argument("--no-ui", action="store_true", help="do not start the dashboard server")
    run.add_argument("--no-browser", action="store_true", help="start the dashboard but do not open a window")
    run.add_argument("--window", choices=["app", "browser"], default=os.environ.get("ALPHA_WINDOW", "app"),
                     help="app = frameless Edge/Chrome window (default), browser = normal tab")
    run.add_argument("--host", default=os.environ.get("ALPHA_WEB_HOST", "127.0.0.1"), help="dashboard address")
    run.add_argument("--port", type=int, default=int(env_float("ALPHA_WEB_PORT", 8765)), help="dashboard port")

    en = add("enroll", cmd_enroll, "record 3-5 phrases and create your voice profile")
    en.add_argument("--samples", type=samples_type, default=5, help="number of phrases, 3-8 (default 5, 8 recommended for a more reliable voice lock)")
    en.add_argument("--impostor", action="store_true", help="also record another person to calibrate the threshold")

    vt = add("voice-test", cmd_voice_test, "live match scores for whoever speaks (try other people too)")
    vt.add_argument("--seconds", type=float, default=30.0, help="how long to listen (default 30)")

    add("voice-status", cmd_voice_status, "show the voice profile")
    add("voice-reset", cmd_voice_reset, "delete the voice profile")
    add("mic-test", cmd_mic_test, "12-second microphone level and recognition test")
    add("list-mics", cmd_list_mics, "list audio input devices")
    add("tools", cmd_tools, "list every registered tool and any custom-tool load errors")
    add("doctor", cmd_doctor, "check Python, files, packages, microphone, ports; prints how to fix problems")
    mg = add("migrate", cmd_migrate, "import settings, memory and custom tools from an older Alpha folder")
    mg.add_argument("old_dir", help="path of the older Alpha folder")
    return parser


def normalize_argv(argv: List[str]) -> List[str]:
    """`python main.py` and `python main.py --text` mean `run`; a command may be preceded by options (`-v tools`)."""
    if not argv:
        return ["run"]
    if argv[0] in ("-h", "--help"):
        return argv
    for i, token in enumerate(argv):
        if token in COMMANDS:
            return argv if i == 0 else [token] + argv[:i] + argv[i + 1:]
    return ["run"] + argv if argv[0].startswith("-") else argv       # unknown word: argparse reports it


# =============================================================================
def main(argv: Optional[List[str]] = None) -> int:
    utf8_console()
    parser = build_parser()
    args = parser.parse_args(normalize_argv(list(sys.argv[1:] if argv is None else argv)))
    verbose = getattr(args, "verbose", False)
    try:
        return int(args.handler(args) or 0)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception as exc:
        from core.instance import AlreadyRunning

        if isinstance(exc, AlreadyRunning):
            print(f"[!] {exc}.\n    Close the other Alpha first (Ctrl+C in its window, or end python.exe in Task Manager), "
                  "then start again. Two copies would fight over the microphone and the dashboard port.")
            return 2
        if isinstance(exc, ImportError):
            print(f"[FAIL] A required package is missing or broken: {exc}\n"
                  "       Fix: activate the virtual environment, then  pip install -r requirements.txt\n"
                  "       Then run  python main.py doctor  to check everything.")
        else:
            print(f"[FAIL] {args.command} stopped: {type(exc).__name__}: {exc}\n"
                  f"       Details: {LOG_FILE}   |   Check your setup with: python main.py doctor")
        logging.getLogger("alpha").exception("Command %s failed", args.command)
        if verbose:
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())