"""
pc_controller.py - Everything Alpha AI can *do* to the PC.

  * ActivityTracker  - background thread logging the foreground app + duration
  * PCController     - power actions, volume, app launching, file operations,
                       fast file search, project memory, screen-time summaries

Every public PCController method returns a short, speakable string.
Dangerous operations (delete, shutdown, restart) require confirmed=True; the
orchestrator is responsible for asking the user by voice first.
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psutil

from core.memory import MemoryManager

log = logging.getLogger("alpha.pc")

IS_WIN = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


# =============================================================================
# Helpers
# =============================================================================
def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    hours, minutes = divmod(seconds // 60, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return " ".join(parts)


FRIENDLY_APP_NAMES = {
    "chrome": "Google Chrome", "msedge": "Microsoft Edge", "firefox": "Firefox",
    "code": "VS Code", "notepad": "Notepad", "explorer": "File Explorer",
    "winword": "Word", "excel": "Excel", "powerpnt": "PowerPoint",
    "cmd": "Command Prompt", "windowsterminal": "Windows Terminal",
    "powershell": "PowerShell", "pycharm64": "PyCharm", "spotify": "Spotify",
    "whatsapp": "WhatsApp", "vlc": "VLC", "taskmgr": "Task Manager",
    "applicationframehost": "Windows App", "systemsettings": "Settings",
    "discord": "Discord", "telegram": "Telegram", "teams": "Microsoft Teams",
}
IGNORED_APPS = {"lockapp", "logonui", "searchhost", "startmenuexperiencehost", "shellexperiencehost"}


def friendly_app(process_name: str) -> str:
    stem = re.sub(r"\.exe$", "", process_name or "unknown", flags=re.I)
    return FRIENDLY_APP_NAMES.get(stem.lower(), stem.capitalize() if stem.islower() else stem)


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def get_idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input (Windows only)."""
    if not IS_WIN:
        return 0.0
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        kernel32 = ctypes.windll.kernel32
        kernel32.GetTickCount.restype = ctypes.c_uint
        millis = (kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
        return millis / 1000.0
    except Exception:
        return 0.0


def get_active_window() -> Optional[Tuple[str, str]]:
    """(friendly app name, window title) of the foreground window, or None."""
    if not IS_WIN:
        return None
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            proc_name = psutil.Process(pid.value).name()
        except psutil.Error:
            proc_name = "unknown"
        if re.sub(r"\.exe$", "", proc_name, flags=re.I).lower() in IGNORED_APPS:
            return None
        return friendly_app(proc_name), title
    except Exception:
        try:  # pygetwindow fallback
            import pygetwindow as gw

            win = gw.getActiveWindow()
            if win and win.title:
                return "unknown", win.title.strip()
        except Exception:
            pass
        return None


# =============================================================================
# Activity tracking
# =============================================================================
class ActivityTracker(threading.Thread):
    """Logs foreground app usage; pauses counting while the user is idle."""

    def __init__(self, memory: MemoryManager, poll: float = 2.0, idle_threshold: float = 120.0,
                 flush_every: float = 60.0, log_titles: bool = True, writer=None, barrier=None):
        super().__init__(name="ActivityTracker", daemon=True)
        self.memory = memory
        self.writer = writer or memory.log_activity   # e.g. a BackgroundHub "db" queue submitter
        self.barrier = barrier                        # waits for queued writes before summaries
        self.poll = poll
        self.idle_threshold = idle_threshold
        self.flush_every = flush_every
        self.log_titles = log_titles
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._key: Optional[Tuple[str, str]] = None
        self._start = 0.0

    def run(self) -> None:
        log.info("Activity tracker started (poll=%.1fs, idle=%.0fs)", self.poll, self.idle_threshold)
        while not self._halt.wait(self.poll):
            try:
                self._tick()
            except Exception:
                log.exception("Activity tracker tick failed")
        with self._lock:
            self._close(time.time(), keep_open=False)

    def stop(self) -> None:
        self._halt.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=3)

    def flush(self, wait: bool = True) -> None:
        """Write the in-progress segment to the DB (used before summaries)."""
        with self._lock:
            if self._key:
                self._close(time.time(), keep_open=True)
        if wait and self.barrier:
            self.barrier()

    def snapshot(self) -> Optional[Tuple[str, str, float]]:
        """(app, title, seconds so far) of the in-progress segment, without writing to the DB."""
        with self._lock:
            if not self._key:
                return None
            return self._key[0], self._key[1], max(0.0, time.time() - self._start)

    def _tick(self) -> None:
        now = time.time()
        idle = get_idle_seconds()
        is_idle = idle >= self.idle_threshold
        info = None if is_idle else get_active_window()
        if info and not self.log_titles:
            info = (info[0], "")
        with self._lock:
            if info is None:
                # Don't count the idle period that elapsed before we noticed it.
                self._close(now - (idle if is_idle else 0.0), keep_open=False)
                return
            if info != self._key:
                self._close(now, keep_open=False)
                self._key, self._start = info, now
            elif now - self._start >= self.flush_every:
                self._close(now, keep_open=True)

    def _close(self, end: float, keep_open: bool) -> None:
        if self._key and end - self._start >= 1.0:
            try:
                self.writer(self._key[0], self._key[1], self._start, end)
            except Exception:
                log.exception("Could not persist activity segment")
        if keep_open and self._key:
            self._start = max(end, self._start)
        else:
            self._key = None


# =============================================================================
# PC controller
# =============================================================================
class PCController:
    APP_ALIASES = {
        "notepad": "notepad.exe", "task manager": "taskmgr.exe", "taskmgr": "taskmgr.exe",
        "command prompt": "cmd.exe", "cmd": "cmd.exe", "powershell": "powershell.exe",
        "terminal": "wt.exe", "windows terminal": "wt.exe",
        "on screen keyboard": "osk.exe", "on-screen keyboard": "osk.exe",
        "onscreen keyboard": "osk.exe", "screen keyboard": "osk.exe", "osk": "osk.exe",
        "keyboard": "osk.exe", "calculator": "calc.exe", "calc": "calc.exe",
        "paint": "mspaint.exe", "file explorer": "explorer.exe", "explorer": "explorer.exe",
        "files": "explorer.exe", "control panel": "control.exe", "settings": "ms-settings:",
        "snipping tool": "snippingtool.exe", "chrome": "chrome", "google chrome": "chrome",
        "edge": "msedge", "microsoft edge": "msedge", "firefox": "firefox",
        "word": "winword", "microsoft word": "winword", "excel": "excel",
        "microsoft excel": "excel", "powerpoint": "powerpnt", "vs code": "code",
        "vscode": "code", "visual studio code": "code", "spotify": "spotify",
    }
    SKIP_DIRS = {
        "windows", "windows.old", "program files", "program files (x86)", "programdata",
        "$recycle.bin", "system volume information", "appdata", "node_modules",
        "__pycache__", "site-packages", "venv", ".venv", "winsxs", "recovery", "msocache",
    }
    SPECIAL_FOLDERS = {"desktop", "documents", "downloads", "pictures", "music", "videos"}
    _UNSAFE_LAUNCH_CHARS = re.compile(r'[&|<>^%"`;]')

    def __init__(self, memory: MemoryManager, tracker: Optional[ActivityTracker] = None):
        self.memory = memory
        self.tracker = tracker
        self._sm_index: Optional[Dict[str, str]] = None
        self._sm_built = 0.0
        self._endpoint_volume = None
        self._endpoint_tried = False

    # ------------------------------------------------------------------ power
    def system_action(self, action: str, confirmed: bool = False) -> str:
        action = (action or "").lower().strip()
        if not IS_WIN:
            return "System actions are only available on Windows."
        if action in ("shutdown", "restart") and not confirmed:
            return "CONFIRMATION_REQUIRED"
        try:
            if action == "shutdown":
                subprocess.run(["shutdown", "/s", "/t", "10"], check=True, creationflags=NO_WINDOW)
                return "Shutting down in 10 seconds. Say cancel shutdown to stop it."
            if action == "restart":
                subprocess.run(["shutdown", "/r", "/t", "10"], check=True, creationflags=NO_WINDOW)
                return "Restarting in 10 seconds. Say cancel restart to stop it."
            if action == "cancel_shutdown":
                result = subprocess.run(["shutdown", "/a"], creationflags=NO_WINDOW)
                return "Cancelled." if result.returncode == 0 else "There was no shutdown in progress."
            if action == "sleep":
                subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], creationflags=NO_WINDOW)
                return "Going to sleep."
            if action == "lock":
                subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"], creationflags=NO_WINDOW)
                return "Locking your PC."
        except (subprocess.SubprocessError, OSError) as exc:
            log.exception("system_action failed")
            return f"I couldn't do that: {exc}"
        return f"I don't know the system action '{action}'."

    # ----------------------------------------------------------------- volume
    def _press(self, key: str, presses: int = 1) -> None:
        import pyautogui

        pyautogui.press(key, presses=presses, interval=0.01)

    def _get_endpoint(self):
        """Exact volume control through pycaw if installed (optional)."""
        if self._endpoint_tried:
            return self._endpoint_volume
        self._endpoint_tried = True
        try:
            from ctypes import POINTER, cast

            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

            speakers = AudioUtilities.GetSpeakers()
            if hasattr(speakers, "EndpointVolume"):  # newer pycaw
                self._endpoint_volume = speakers.EndpointVolume
            else:
                iface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                self._endpoint_volume = cast(iface, POINTER(IAudioEndpointVolume))
        except Exception as exc:
            log.info("pycaw unavailable (%s); using media keys for volume.", exc)
            self._endpoint_volume = None
        return self._endpoint_volume

    def audio(self, action: str, level: Optional[int] = None) -> str:
        action = (action or "").lower().strip()
        ep = self._get_endpoint()
        try:
            if action in ("mute", "unmute"):
                if ep is not None:
                    ep.SetMute(1 if action == "mute" else 0, None)
                    return "Muted." if action == "mute" else "Unmuted."
                self._press("volumemute")
                return "Toggled mute."
            if action == "toggle_mute":
                if ep is not None:
                    ep.SetMute(0 if ep.GetMute() else 1, None)
                else:
                    self._press("volumemute")
                return "Toggled mute."
            if action in ("volume_up", "volume_down"):
                step = 0.10 if action == "volume_up" else -0.10
                if ep is not None:
                    cur = ep.GetMasterVolumeLevelScalar()
                    new = min(1.0, max(0.0, cur + step))
                    ep.SetMasterVolumeLevelScalar(new, None)
                    return f"Volume {round(new * 100)} percent."
                self._press("volumeup" if step > 0 else "volumedown", presses=5)  # ~2% per press
                return "Volume turned up." if step > 0 else "Volume turned down."
            if action == "set_volume":
                if level is None:
                    return "What volume level should I set, from 0 to 100?"
                level = max(0, min(100, int(level)))
                if ep is not None:
                    ep.SetMasterVolumeLevelScalar(level / 100.0, None)
                else:
                    self._press("volumedown", presses=50)
                    self._press("volumeup", presses=round(level / 2))
                return f"Volume set to {level} percent."
        except Exception as exc:
            log.exception("audio control failed")
            self._endpoint_volume = None  # fall back to keys next time
            return f"I couldn't change the audio: {exc}"
        return f"I don't know the audio action '{action}'."

    # ------------------------------------------------------------ app launching
    def open_app(self, name: str) -> str:
        raw = (name or "").strip()
        if not raw:
            return "Which application should I open?"
        if self._UNSAFE_LAUNCH_CHARS.search(raw):
            return "That application name contains characters I won't run."
        key = re.sub(r"^(the|my)\s+|\s+(app|application|program|software)$", "", raw.lower()).strip()
        target = self.APP_ALIASES.get(key, key)
        try:
            if self._launch(target, key):
                return f"Opening {raw}."
        except Exception as exc:
            log.exception("open_app failed")
            return f"I couldn't open {raw}: {exc}"
        return f"I couldn't find an application called {raw}."

    def _launch(self, target: str, spoken: str) -> bool:
        if not IS_WIN:
            return False
        if target.endswith(":"):  # URI such as ms-settings:
            os.startfile(target)  # type: ignore[attr-defined]
            return True
        if target.lower() == "cmd.exe":
            subprocess.Popen(["cmd.exe"], creationflags=NEW_CONSOLE)
            return True
        if target.lower() == "osk.exe":  # 32-bit Python on 64-bit Windows cannot see System32 directly
            windir = os.environ.get("WINDIR", r"C:\Windows")
            for cand in (os.path.join(windir, "System32", "osk.exe"), os.path.join(windir, "Sysnative", "osk.exe")):
                if os.path.exists(cand) and self._start_exe(cand):
                    return True
            return self._shell_execute("open", "osk.exe") or self._shell_execute("runas", "osk.exe")
        exe = shutil.which(target) or self._app_paths_lookup(target)
        if exe:
            return self._start_exe(exe)
        shortcut = self._find_start_menu_shortcut(spoken)
        if shortcut:
            os.startfile(shortcut)  # type: ignore[attr-defined]
            return True
        app_id = self._find_store_app(spoken)  # UWP / Microsoft Store apps
        if app_id:
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"])
            return True
        return False

    @staticmethod
    def _shell_execute(verb: str, path: str, params: Optional[str] = None) -> bool:
        """ShellExecuteW: verb 'open' or 'runas' (shows the Windows UAC prompt for administrator rights)."""
        try:
            return ctypes.windll.shell32.ShellExecuteW(None, verb, path, params, None, 1) > 32
        except Exception:
            log.exception("ShellExecute(%s, %s) failed", verb, path)
            return False

    def _start_exe(self, exe: str) -> bool:
        try:
            subprocess.Popen([exe], creationflags=NO_WINDOW if not exe.lower().endswith(".exe") else 0)
            return True
        except OSError as exc:
            if getattr(exc, "winerror", None) == 740:  # "requires elevation"
                log.info("%s needs administrator rights; asking Windows (UAC prompt).", exe)
                return self._shell_execute("runas", exe)
            log.info("Direct launch of %s failed (%s); trying ShellExecute.", exe, exc)
            return self._shell_execute("open", exe)

    @staticmethod
    def _app_paths_lookup(target: str) -> Optional[str]:
        try:
            import winreg
        except ImportError:
            return None
        exe_name = target if target.lower().endswith(".exe") else target + ".exe"
        sub = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    path, _ = winreg.QueryValueEx(k, None)
                    path = os.path.expandvars(str(path)).strip('"')
                    if os.path.exists(path):
                        return path
            except OSError:
                continue
        return None

    def _start_menu_index(self) -> Dict[str, str]:
        if self._sm_index is None or (time.time() - self._sm_built > 600):
            index: Dict[str, str] = {}
            roots = [
                Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
                Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
            ]
            for root in roots:
                if not root.exists():
                    continue
                for dirpath, _, files in os.walk(root):
                    for f in files:
                        if f.lower().endswith((".lnk", ".url")):
                            index[Path(f).stem.lower()] = os.path.join(dirpath, f)
            self._sm_index, self._sm_built = index, time.time()
        return self._sm_index

    def _find_start_menu_shortcut(self, spoken: str) -> Optional[str]:
        idx = self._start_menu_index()
        spoken = spoken.lower()
        if spoken in idx:
            return idx[spoken]
        tokens = spoken.split()
        matches = [s for s in idx if all(t in s for t in tokens)]
        return idx[min(matches, key=len)] if matches else None

    @staticmethod
    def _find_store_app(spoken: str) -> Optional[str]:
        safe = re.sub(r"[^\w\s.\-]", "", spoken)
        if not safe:
            return None
        ps = ("Get-StartApps | Where-Object { $_.Name -like '*" + safe + "*' } "
              "| Select-Object -First 1 | ForEach-Object { $_.AppID }")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                                 capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
            app_id = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
            return app_id or None
        except (subprocess.SubprocessError, OSError, IndexError):
            return None

    # ------------------------------------------------------------ path helpers
    @staticmethod
    def _special_dir(name: str) -> Path:
        home = Path.home()
        for cand in (home / "OneDrive" / name, home / name):  # OneDrive-redirected folders first
            if cand.exists():
                return cand
        return home / name

    def resolve_path(self, raw: str) -> Path:
        raw = (raw or "").strip().strip('"').strip("'")
        expanded = os.path.expandvars(os.path.expanduser(raw))
        parts = re.split(r"[\\/]", expanded, maxsplit=1)
        if parts[0].lower() in self.SPECIAL_FOLDERS:
            base = self._special_dir(parts[0].capitalize())
            return base / parts[1] if len(parts) > 1 and parts[1] else base
        p = Path(expanded)
        return p if p.is_absolute() else self._special_dir("Desktop") / p

    def _is_protected(self, p: Path) -> bool:
        try:
            rp = p.resolve()
        except OSError:
            return True
        if rp.parent == rp:  # drive root
            return True
        home = Path.home().resolve()
        exact = {home, home.parent, home / "OneDrive"}
        exact |= {(home / n) for n in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")}
        if rp in exact:
            return True
        for env in ("WINDIR", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            val = os.environ.get(env)
            if val:
                base = Path(val).resolve()
                if rp == base or base in rp.parents:
                    return True
        return False

    # ------------------------------------------------------- window management
    def window_control(self, action: str, title: str = "", confirmed: bool = False) -> str:
        action = (action or "").lower().strip()
        if not IS_WIN:
            return "Window control is only available on Windows."
        if action == "minimize_all":
            self._press_hotkey("win", "d")
            return "Showing the desktop."
        try:
            import pygetwindow as gw
        except ImportError:
            return "Window control needs the pygetwindow package."
        wins = [w for w in gw.getAllWindows() if (w.title or "").strip()]
        if action == "list":
            names = [w.title.strip() for w in wins][:8]
            return ("Open windows: " + "; ".join(names) + ".") if names else "I can't see any open windows."
        if action not in ("focus", "minimize", "maximize", "restore", "close"):
            return f"I don't know the window action '{action}'."
        needle = (title or "").lower().strip()
        matches = [w for w in wins if needle and needle in w.title.lower()]
        if not matches:
            return f"I couldn't find a window matching {title or 'that'}."
        win = matches[0]
        if action == "close" and not confirmed:
            return "CONFIRMATION_REQUIRED"
        try:
            if action == "focus":
                try:
                    if win.isMinimized:
                        win.restore()
                    win.activate()
                except Exception:  # pygetwindow raises odd "error code 0" on some systems
                    win.minimize()
                    win.restore()
            elif action == "minimize":
                win.minimize()
            elif action == "maximize":
                win.maximize()
            elif action == "restore":
                win.restore()
            else:
                win.close()
        except Exception as exc:
            log.exception("window_control failed")
            return f"I couldn't {action} that window: {exc}"
        return f"Done: {action} {win.title.strip()[:60]}."

    @staticmethod
    def _press_hotkey(*keys: str) -> None:
        import pyautogui

        pyautogui.hotkey(*keys)

    # ----------------------------------------------------------- file operations
    def create_folder(self, path: str) -> str:
        p = self.resolve_path(path)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return f"Created the folder {p}."
        except OSError as exc:
            return f"I couldn't create that folder: {exc}"

    def create_file(self, path: str, content: str = "") -> str:
        p = self.resolve_path(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "x", encoding="utf-8") as fh:  # 'x' never overwrites
                fh.write(content or "")
            return f"Created the file {p}."
        except FileExistsError:
            return f"{p.name} already exists, so I left it untouched."
        except OSError as exc:
            return f"I couldn't create that file: {exc}"

    def move_path(self, source: str, destination: str) -> str:
        src, dst = self.resolve_path(source), self.resolve_path(destination)
        if not src.exists():
            return f"I couldn't find {src}."
        if self._is_protected(src):
            return f"For safety I won't move {src}."
        # "move report.docx to Backup" means *into* a folder, even one that doesn't exist yet.
        into_folder = dst.is_dir() or (not dst.exists() and not dst.suffix and src.is_file())
        target = dst / src.name if into_folder else dst
        if target.exists():
            return f"{target} already exists, so I didn't move anything."
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))
            return f"Moved {src.name} to {target.parent}."
        except (OSError, shutil.Error) as exc:
            return f"I couldn't move it: {exc}"

    def delete_path(self, path: str, confirmed: bool = False) -> str:
        p = self.resolve_path(path)
        if not p.exists():
            hits = self.search_files(Path(path).name or path, limit=3)
            hint = f" Did you mean: {'; '.join(hits)}?" if hits else ""
            return f"I couldn't find {p}.{hint}"
        if self._is_protected(p):
            return f"For safety I won't delete {p}."
        if not confirmed:
            return "CONFIRMATION_REQUIRED"
        try:
            from send2trash import send2trash
        except ImportError:
            return "Safe delete needs the send2trash package. Run: pip install send2trash."
        try:
            send2trash(str(p.resolve()))
            return f"Moved {p.name} to the Recycle Bin."
        except Exception as exc:
            log.exception("delete failed")
            return f"I couldn't delete it: {exc}"

    # ----------------------------------------------------------------- search
    def search_files(self, query: str, limit: int = 5, dirs_only: bool = False,
                     timeout: float = 12.0) -> List[str]:
        query = (query or "").strip()
        if not query:
            return []
        hits = self._search_everything(query, limit, dirs_only)
        return hits if hits else self._search_walk(query, limit, dirs_only, timeout)

    def search_files_text(self, query: str) -> str:
        hits = self.search_files(query, limit=5)
        if not hits:
            return f"I couldn't find anything matching {query}."
        return f"I found {len(hits)} result{'s' if len(hits) != 1 else ''}: " + "; ".join(hits)

    @staticmethod
    def _search_everything(query: str, limit: int, dirs_only: bool) -> List[str]:
        """Instant search through voidtools Everything's es.exe CLI, if installed."""
        exe = shutil.which("es") or shutil.which("es.exe")
        if not exe:
            return []
        cmd = [exe, "-n", str(limit)] + (["/ad"] if dirs_only else []) + [query]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                                 timeout=5, creationflags=NO_WINDOW)
        except (subprocess.SubprocessError, OSError):
            return []
        if out.returncode != 0:
            return []
        return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()][:limit]

    def _search_roots(self) -> List[str]:
        roots = [str(self._special_dir(n)) for n in ("Desktop", "Documents", "Downloads")]
        try:
            for part in psutil.disk_partitions(all=False):
                if (not IS_WIN) or "fixed" in part.opts:
                    roots.append(part.mountpoint)
        except Exception:
            pass
        seen, ordered = set(), []
        for r in roots:
            if r not in seen and os.path.isdir(r):
                seen.add(r)
                ordered.append(r)
        return ordered

    def _search_walk(self, query: str, limit: int, dirs_only: bool, timeout: float) -> List[str]:
        tokens = [t for t in query.lower().split() if t]
        deadline = time.time() + timeout
        results: List[str] = []
        seen = set()
        for root in self._search_roots():
            for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=lambda e: None):
                dirnames[:] = [d for d in dirnames
                               if d.lower() not in self.SKIP_DIRS and not d.startswith(".")]
                candidates = list(dirnames) + ([] if dirs_only else list(filenames))
                for name in candidates:
                    low = name.lower()
                    if all(t in low for t in tokens):
                        full = os.path.join(dirpath, name)
                        if full not in seen:
                            seen.add(full)
                            results.append(full)
                            if len(results) >= limit:
                                return results
                if time.time() > deadline:
                    return results
        return results

    # --------------------------------------------------------- project memory
    def save_project(self, name: str, path: str) -> str:
        if not (name or "").strip() or not (path or "").strip():
            return "I need both a project name and a folder path."
        resolved = self.resolve_path(path)
        if not resolved.exists():
            return f"I couldn't find {resolved}, so I didn't save it."
        try:
            self.memory.save_project(name, str(resolved))
            return f"Saved. {name.strip()} is at {resolved}."
        except Exception as exc:
            return f"I couldn't save that project: {exc}"

    def locate_project(self, name: str) -> str:
        rows = self.memory.find_projects(name)
        if rows:
            lines = []
            for r in rows[:3]:
                note = "" if r["exists"] else " (that folder no longer exists)"
                lines.append(f"{r['name']} is at {r['path']}{note}")
            return "; ".join(lines) + "."
        hits = self.search_files(name, limit=5, dirs_only=True)
        if not hits:
            return f"I don't have a project called {name} saved, and I couldn't find a matching folder."
        return ("It's not saved yet, but I found these folders: " + "; ".join(hits[:3])
                + ". Tell me which one to remember.")

    def list_projects(self) -> str:
        rows = self.memory.list_projects()
        if not rows:
            return "You haven't saved any projects yet."
        return "Saved projects: " + "; ".join(f"{r['name']} at {r['path']}" for r in rows) + "."

    def add_note(self, text: str) -> str:
        if not (text or "").strip():
            return "What should the note say?"
        self.memory.add_note(text)
        return "Noted."

    def get_notes(self) -> str:
        notes = self.memory.get_notes(limit=5)
        if not notes:
            return "You have no saved notes."
        return "Your latest notes: " + " | ".join(
            f"{datetime.fromtimestamp(n['created_at']):%b %d}: {n['text']}" for n in notes)

    # ------------------------------------------------------------- screen time
    def screen_time(self, period: str = "today") -> str:
        if self.tracker is None:
            return "Activity tracking is turned off."
        self.tracker.flush()
        today = date.today()
        period = (period or "today").lower()
        if period == "yesterday":
            start = end = today - timedelta(days=1)
            label = "yesterday"
        elif period in ("week", "this week", "last 7 days"):
            start, end, label = today - timedelta(days=6), today, "over the last 7 days"
        else:
            start = end = today
            label = "today"
        summary = self.memory.activity_summary(start.isoformat(), end.isoformat())
        if summary["total"] < 30:
            return f"I don't have any meaningful screen time recorded for {label} yet."
        top = ", ".join(f"{app} {format_duration(sec)}" for app, sec in summary["apps"][:5])
        return f"Your screen time {label} is {format_duration(summary['total'])}. Most used: {top}."
