r"""
scripts/migrate_old_project.py - bring an older Alpha folder (v1-v3) into this project.

    python main.py migrate C:\path\to\old_alpha_folder

  1. .env            keys are merged into ./.env (existing values here are kept; nothing is deleted)
                     ALPHA_WEB_TOKEN becomes SERVER_TOKEN; obsolete Streamlit settings are skipped
  2. alpha_memory.db copied to data/ (your notes, remembered projects, screen-time history, chat log);
                     an existing database here is backed up first
  3. custom tools    every top-level function in the old folder's other .py files gets a DRAFT wrapper in
                     tools/custom/migrated_<file>.py.draft. Review it, then rename it to .py to enable it.
                     Nothing from the old folder is executed during migration.
  4. voice profile   old versions had none; enroll with:  python main.py enroll
"""
from __future__ import annotations

import ast
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from config.settings import DATA_DIR, ROOT

KNOWN_ALPHA_FILES = {"alpha_main.py", "agents.py", "brains.py", "background.py", "session.py", "n8n_client.py", "dashboard.py",
                     "voice_engine.py", "pc_controller.py", "memory_manager.py", "app.py", "main.py", "setup.py", "conftest.py"}
OBSOLETE = {"ALPHA_UI_PORT", "ALPHA_UI", "ALPHA_API_URL", "ALPHA_SESSION_FILE", "ALPHA_ALLOW_NULL_ORIGIN", "ALPHA_ALLOWED_ORIGINS"}
RENAMED = {"ALPHA_WEB_TOKEN": "SERVER_TOKEN"}
_PLACEHOLDER = re.compile(r"^(paste|your|<|xxx)", re.I)


def parse_env(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.split(" #")[0].strip().strip('"').strip("'")
    return out


def merge_env(old_env: Path, new_env: Path, example: Path) -> List[str]:
    """Add the old values to ./.env. Returns human-readable notes."""
    notes: List[str] = []
    if not new_env.exists() and example.exists():
        shutil.copy(example, new_env)
        notes.append("created .env from .env.example")
    text = new_env.read_text(encoding="utf-8-sig") if new_env.exists() else ""
    current = parse_env(new_env) if new_env.exists() else {}
    added = 0
    for key, value in parse_env(old_env).items():
        key = RENAMED.get(key, key)
        if key in OBSOLETE or not value or _PLACEHOLDER.match(value):
            continue
        if current.get(key) and not _PLACEHOLDER.match(current[key]):   # keep real values already set here
            continue
        pattern = re.compile(rf"^#?\s*{re.escape(key)}\s*=.*$", re.M)
        line = f"{key}={value}"
        if pattern.search(text):
            text = pattern.sub(lambda m: line, text, count=1)
        else:
            text += ("" if text.endswith("\n") or not text else "\n") + line + "\n"
        added += 1
        notes.append(f"  + {key}" + (" (renamed from ALPHA_WEB_TOKEN)" if key == "SERVER_TOKEN" and "ALPHA_WEB_TOKEN" in parse_env(old_env) else ""))
    new_env.write_text(text, encoding="utf-8")
    notes.append(f"merged {added} setting(s) into {new_env.name}")
    return notes


def scan_functions(path: Path) -> List[dict]:
    """Top-level public functions of a file, via AST only (the file is never imported or run)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
    except SyntaxError:
        return []
    funcs = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            args = []
            defaults = [None] * (len(node.args.args) - len(node.args.defaults)) + list(node.args.defaults)
            for a, d in zip(node.args.args, defaults):
                if a.arg in ("self", "cls"):
                    continue
                ann = ast.unparse(a.annotation) if a.annotation is not None else ""
                typ = {"int": "integer", "float": "number", "bool": "boolean"}.get(ann, "string")
                args.append({"name": a.arg, "type": typ, "required": d is None})
            funcs.append({"name": node.name, "doc": (ast.get_docstring(node) or "").splitlines()[0] if ast.get_docstring(node) else "",
                          "args": args})
    return funcs


def draft_for(old_file: Path, funcs: List[dict]) -> str:
    mod = re.sub(r"\W", "_", old_file.stem)
    lines = [
        f'"""DRAFT migrated from {old_file}. Review each wrapper, then rename this file to .py to enable it."""',
        "import importlib.util",
        "from core.registry import tool",
        "",
        f"_LEGACY_PATH = {str(old_file)!r}",
        "_mod = None",
        "",
        "",
        "def _legacy():",
        "    global _mod",
        "    if _mod is None:",
        f"        spec = importlib.util.spec_from_file_location('legacy_{mod}', _LEGACY_PATH)",
        "        _mod = importlib.util.module_from_spec(spec)",
        "        spec.loader.exec_module(_mod)",
        "    return _mod",
        "",
    ]
    for f in funcs:
        params = "{" + ", ".join(f'"{a["name"]}": {{"type": "{a["type"]}"}}' for a in f["args"]) + "}"
        req = "[" + ", ".join(f'"{a["name"]}"' for a in f["args"] if a["required"]) + "]"
        sig = "".join(f", {a['name']}" + ("" if a["required"] else "=None") for a in f["args"])
        kw = "{" + ", ".join(f'"{a["name"]}": {a["name"]}' for a in f["args"]) + "}"   # omitted optionals keep the old defaults
        desc = f["doc"] or f"Legacy function {f['name']} from {old_file.name}."
        lines += ["", f"@tool({('legacy_' + f['name'])!r}, {desc!r}, params={params}, required={req}, agent='Legacy tools')",
                  f"def legacy_{f['name']}(ctx{sig}) -> str:",
                  f"    ctx.step('Running legacy function {f['name']}')",
                  f"    kwargs = {{k: v for k, v in {kw}.items() if v is not None}}",
                  f"    return str(_legacy().{f['name']}(**kwargs))", ""]
    return "\n".join(lines)


def migrate(old_dir: str) -> int:
    old = Path(old_dir).expanduser().resolve()
    if not old.is_dir():
        print(f"Folder not found: {old}")
        return 1
    print(f"Migrating from {old}\n")
    # 1. settings
    old_env = old / ".env"
    if old_env.exists():
        print("[1/4] Settings (.env)")
        for n in merge_env(old_env, ROOT / ".env", ROOT / ".env.example"):
            print("   ", n)
    else:
        print("[1/4] No .env found in the old folder (nothing to merge).")
    # 2. memory
    old_db = next((p for p in (old / "alpha_memory.db", old / "data" / "alpha_memory.db") if p.exists()), None)
    print("\n[2/4] Memory database")
    if old_db:
        dst = DATA_DIR / "alpha_memory.db"
        if dst.exists():
            backup = dst.with_name(f"alpha_memory.backup-{int(time.time())}.db")
            shutil.copy2(dst, backup)
            print(f"    existing database backed up to {backup.name}")
        shutil.copy2(old_db, dst)
        print(f"    copied {old_db.name} -> {dst}")
    else:
        print("    none found.")
    # 3. custom tools
    print("\n[3/4] Custom tools")
    made = 0
    for f in sorted(old.glob("*.py")):
        if f.name in KNOWN_ALPHA_FILES or f.name.startswith(("test_", "_")):
            continue
        funcs = scan_functions(f)
        if not funcs:
            continue
        safe = re.sub(r"\W", "_", f.stem)
        out = ROOT / "tools" / "custom" / f"migrated_{safe}.py.draft"
        out.write_text(draft_for(f, funcs), encoding="utf-8")
        made += 1
        print(f"    {f.name}: {len(funcs)} function(s) -> {out.relative_to(ROOT)}")
    if not made:
        print("    no extra .py files with functions found (nothing to port).")
    else:
        print("    Review each .draft, rename to .py, then run: python main.py tools")
    print("\n[4/4] Voice profile: old versions had none. Create yours with:  python main.py enroll")
    print("\nDone. Start Alpha with:  python main.py")
    return 0


if __name__ == "__main__":
    sys.exit(migrate(sys.argv[1] if len(sys.argv) > 1 else "."))
