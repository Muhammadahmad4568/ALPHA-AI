"""config/quick_actions.py - one-click buttons in the dashboard. Each runs a registered tool directly (no LLM)."""
QUICK_ACTIONS = [
    {"id": "task_manager", "label": "Task Manager", "icon": "\U0001F4CA", "tool": "open_app", "args": {"name": "task manager"}},
    {"id": "keyboard", "label": "On-Screen Keyboard", "icon": "\u2328\uFE0F", "tool": "open_app", "args": {"name": "on screen keyboard"}},
    {"id": "notepad", "label": "Notepad", "icon": "\U0001F4DD", "tool": "open_app", "args": {"name": "notepad"}},
    {"id": "chrome", "label": "Chrome", "icon": "\U0001F310", "tool": "open_app", "args": {"name": "chrome"}},
    {"id": "screen_time", "label": "Screen Time", "icon": "\u23F1\uFE0F", "tool": "screen_time", "args": {"period": "today"}},
    {"id": "productivity", "label": "Productivity", "icon": "\U0001F3AF", "tool": "productivity_report", "args": {"period": "today"}},
    {"id": "vol_up", "label": "Volume Up", "icon": "\U0001F50A", "tool": "audio_control", "args": {"action": "volume_up"}},
    {"id": "vol_down", "label": "Volume Down", "icon": "\U0001F509", "tool": "audio_control", "args": {"action": "volume_down"}},
    {"id": "mute_pc", "label": "Mute / Unmute PC", "icon": "\U0001F507", "tool": "audio_control", "args": {"action": "toggle_mute"}},
    {"id": "desktop", "label": "Show Desktop", "icon": "\U0001F5A5\uFE0F", "tool": "window_control", "args": {"action": "minimize_all"}},
    {"id": "projects", "label": "My Projects", "icon": "\U0001F4C1", "tool": "list_projects", "args": {}},
    {"id": "notes", "label": "My Notes", "icon": "\U0001F5D2\uFE0F", "tool": "get_notes", "args": {}},
    {"id": "lock", "label": "Lock PC", "icon": "\U0001F512", "tool": "system_action", "args": {"action": "lock"}},
    {"id": "sleep", "label": "Sleep", "icon": "\U0001F319", "tool": "system_action", "args": {"action": "sleep"}},
]
QUICK_BY_ID = {q["id"]: q for q in QUICK_ACTIONS}
