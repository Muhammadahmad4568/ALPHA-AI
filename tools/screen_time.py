"""tools/screen_time.py - Screen Time & Activity agent: totals and productivity analytics."""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Dict, Tuple

from core.registry import tool
from tools.pc_controller import format_duration

A = "Screen Time & Activity"
PERIOD = {"type": "string", "enum": ["today", "yesterday", "week"]}

CATEGORY_RULES = [
    ("Entertainment", ("spotify", "vlc", "netflix", "youtube", "twitch", "steam", "game", "prime video", "tiktok",
                       "facebook", "instagram", "reddit", "twitter")),
    ("Development", ("code", "pycharm", "intellij", "studio", "terminal", "cmd", "command prompt", "powershell",
                     "git", "github", "python", "jupyter", "notepad++", "sublime", "docker", "postman", "stack overflow")),
    ("Office & docs", ("word", "excel", "powerpoint", "onenote", "acrobat", "libreoffice", "notion", "obsidian",
                       "docs", "sheets", "slides")),
    ("Communication", ("whatsapp", "telegram", "teams", "slack", "discord", "outlook", "gmail", "mail", "zoom", "skype")),
    ("Web browsing", ("chrome", "edge", "firefox", "brave", "opera")),
]
PRODUCTIVE = {"Development", "Office & docs"}


def categorize(app: str, title: str) -> str:
    text = f"{app} {title}".lower()
    for category, keys in CATEGORY_RULES:
        if any(re.search(r"\b" + re.escape(k), text) for k in keys):
            return category
    return "Other"


def period_range(period: str) -> Tuple[date, date, str]:
    today = date.today()
    p = (period or "today").lower()
    if p == "yesterday":
        return today - timedelta(days=1), today - timedelta(days=1), "yesterday"
    if p in ("week", "this week", "last 7 days", "7 days"):
        return today - timedelta(days=6), today, "over the last 7 days"
    return today, today, "today"


@tool("screen_time", "Summarise screen time and the most used apps.", params={"period": PERIOD}, agent=A, fast=True,
      title=lambda a: f"Screen time ({a.get('period', 'today')})")
def screen_time(ctx, period: str = "today") -> str:
    if ctx.svc.tracker is None:
        return "Activity tracking is turned off."
    ctx.step("Reading the activity log")
    return ctx.svc.pc.screen_time(period)


@tool("productivity_report", "Productivity analytics: categories (development, office, communication, entertainment, "
      "browsing), focus score, daily average and busiest day.", params={"period": PERIOD}, agent=A, fast=True,
      title=lambda a: f"Productivity report ({a.get('period', 'today')})")
def productivity_report(ctx, period: str = "today") -> str:
    tracker, memory = ctx.svc.tracker, ctx.svc.memory
    if tracker is None:
        return "Activity tracking is turned off."
    ctx.step("Flushing the live activity segment")
    tracker.flush()
    start, end, label = period_range(period)
    ctx.step("Classifying windows into categories")
    rows = memory.activity_titles(start.isoformat(), end.isoformat(), 500)
    total = sum(s for _, _, s in rows)
    if total < 60:
        return f"I don't have enough activity recorded {label} for a report yet."
    cats: Dict[str, float] = {}
    for app, title, secs in rows:
        c = categorize(app, title)
        cats[c] = cats.get(c, 0.0) + secs
    ranked = sorted(cats.items(), key=lambda kv: kv[1], reverse=True)
    focus = sum(v for k, v in cats.items() if k in PRODUCTIVE) / total * 100
    parts = [f"{label.capitalize()} you were active for {format_duration(total)}.",
             "Biggest areas: " + ", ".join(f"{k} {v / total * 100:.0f} percent" for k, v in ranked[:3]) + ".",
             f"Focus score, meaning development and office work, is {focus:.0f} percent."]
    if end > start:
        daily = memory.activity_daily(7)
        busiest = max(daily, key=lambda d: d[1])
        if busiest[1] > 0:
            parts.append(f"Daily average {format_duration(sum(v for _, v in daily) / 7)}; busiest day "
                         f"{date.fromisoformat(busiest[0]).strftime('%A')} with {format_duration(busiest[1])}.")
    return " ".join(parts)
