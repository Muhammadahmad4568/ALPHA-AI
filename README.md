# Alpha AI v4

A voice assistant for Windows 10/11 that **only obeys your voice**, controls your PC, messages people on WhatsApp, reads and summarises web pages, and shows everything it does in a live dashboard.

```
mic → noise gate → VOICE LOCK (is it you?) → Whisper → "Alpha" must come first → Orchestrator
                                                         ├ fast path (rules)       ┐
                                                         ├ Gemini + tools (stream) ├→ tools → steps shown live
                                                         └ offline rules           ┘
                                           → sentence-streamed voice     ·     events → dashboard (SSE)
```

## What changed from the Streamlit version

| Problem | Fix |
|---|---|
| Streamlit "Server offline" | **Streamlit is gone.** The dashboard is a static page fed by **Server-Sent Events**. The browser reconnects by itself, the server resends a full snapshot on every reconnect, and a heartbeat keeps the stream alive. Nothing "reruns", so audio and LLM threads can't break the UI. |
| Dated UI | Orb with 5 colour-coded states (idle, listening, processing, speaking, **executing**), a step-by-step execution panel, activity feed, console log, voice-lock status. |
| Weak automation | WhatsApp messaging (`tools/whatsapp_tool.py`) and a deep web reader (`tools/web_reader.py`). |
| Anyone could command it | Voice biometric lock (`voice_security/`), checked before anything is transcribed or executed. |

## Folder structure

```
AlphaAI/
├─ main.py                    launcher + sub-commands (run, enroll, tools, migrate, mic-test ...)
├─ requirements.txt  .env.example  README.md
├─ config/        settings.py (paths, .env), quick_actions.py, contacts.example.json
├─ core/          assistant.py (conductor) · orchestrator.py · router.py · registry.py (tool registry)
│                 brains.py (Gemini/Claude) · events.py (event bus) · background.py (asyncio queues) · memory.py (SQLite)
├─ voice/         engine.py (mic pipeline, Whisper STT, streamed TTS)
├─ voice_security/ features.py (MFCC+pitch) · voice_auth.py (enroll / verify / CLI)
├─ tools/         system_tools · memory_tools · screen_time · automation (n8n) · whatsapp_tool · web_reader
│                 pc_controller.py · n8n_client.py · custom/ (your own tools)
├─ ui/            server.py (FastAPI + SSE)   static/ (index.html, style.css, app.js)
├─ scripts/       migrate_old_project.py
└─ data/          created at runtime: memory db, voice_profile.npz, whatsapp_profile/, alpha.log
```

## 1. Setup

Install **Python 3.10–3.12 (64-bit)** from python.org (tick *Add python.exe to PATH*). Then, in Command Prompt:

```bat
cd /d C:\AlphaAI
python -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env
notepad .env
```

In `.env` paste your free Gemini key (get it at https://aistudio.google.com/apikey). Windows → *Settings → Privacy & security → Microphone* must allow desktop apps. WhatsApp and the web reader use the **Microsoft Edge already on Windows** (no extra browser download).

Check your microphone: `python main.py mic-test`.

## 2. Voice lock: record your voice (first-time setup)

```bat
python main.py enroll
```

Alpha asks for 5 phrases (3 minimum: `--samples 3`). Stay quiet for 2 s while it calibrates, then read each prompt in your normal voice. A profile is saved to `data\voice_profile.npz` with limits calibrated from *your* samples. Better still, add `--impostor` and let another person say two phrases: the threshold is then set between your voice and theirs.

```bat
python main.py voice-test      speak, then let someone else speak: see each phrase ACCEPTED / REJECTED with its score
python main.py voice-status    profile summary
python main.py voice-reset     delete the profile
```

Until a profile exists, strict mode **refuses voice commands** (typing in the dashboard still works). If *you* get rejected, set `ALPHA_VOICE_STRICTNESS=1.3` in `.env` or re-enroll with more variety. `ALPHA_VOICE_LOCK=warn` only logs; `off` disables.

**What this lock can and cannot do. Please read.**
* The default backend (timbre + pitch statistics) reliably separates voices that differ in pitch or clearly in timbre, such as other family members or a TV. In my synthetic-voice tests it accepted the owner and rejected voices with different pitch, but **a different speaker with a similar pitch and vocal tract could pass**. It is a convenience lock, not strong security.
* A **recording of your voice will pass**: there is no anti-replay check.
* Very short phrases (under about 0.5 s, like a bare "yes") can't be verified. Use the dashboard's Yes/No buttons.
* For real accuracy install `torch` + `speechbrain` and set `ALPHA_VOICE_BACKEND=speechbrain` *before* enrolling (neural ECAPA embeddings, cosine similarity, ~1 GB RAM). I could not test that backend in my build environment.
* The profile is an `.npz` file of numbers (not recordings), loaded without pickle so it can't execute code.

## 3. Run

```bat
venv\Scripts\activate
python main.py
```

A dashboard window opens (`http://127.0.0.1:8765`). Say **"Alpha, open Task Manager"**: the phrase must *start* with "Alpha". Options: `--window browser`, `--text` (typed only), `--mute`, `--whisper tiny`, `--no-ui`, `--port 9000`. `python main.py tools` lists every tool.

## 4. WhatsApp

1. `copy config\contacts.example.json config\contacts.json` and add your contacts (`"ali": "+923001234567"`, groups as `{"chat": "Group name"}`). Set `WHATSAPP_DEFAULT_COUNTRY=92` if you want local numbers like `0300…`.
2. Say: *"Alpha, send WhatsApp to Ali saying I'll be there in ten minutes."* The first time, Edge opens WhatsApp Web: **scan the QR code once** (the login is kept in `data\whatsapp_profile`).
3. Alpha shows and reads out the recipient and text and **always asks yes/no first**. The panel then shows: *Resolving the contact → Opening WhatsApp Web → Opening the chat → Checking the message text → Sending → Verifying delivery*.

Unknown names are refused rather than guessed. Limits: 10 messages/hour, 8 s apart. **Caveats:** WhatsApp's terms restrict unofficial automation and accounts sending automated or bulk messages can be banned, so use it for your own personal, low-volume messages (business use: the official WhatsApp Business Cloud API). WhatsApp Web's page structure changes; I tested the flow with real Chromium against a local imitation, not against whatsapp.com, so selectors may need updating (lists at the top of `whatsapp_tool.py`).

## 5. Web reader

*"Alpha, read https://… and summarise it"*, *"Alpha, search the web for …"*, *"Alpha, what's the latest news about …"*. Static pages are downloaded and cleaned (navigation, ads, cookie banners removed); pages that need JavaScript are rendered in a headless browser automatically. Get a free `TAVILY_API_KEY` for reliable search (otherwise a best-effort DuckDuckGo query is used). Safety: only public http(s) addresses are opened (localhost, your LAN and cloud-metadata addresses are blocked on every redirect), downloads are capped at 2 MB, and page text is handed to the AI as untrusted data. Respect each site's terms of use.

## 6. Add your own tools

Drop a file in `tools/custom/` (see `EXAMPLE_tool.py.txt`):

```python
from core.registry import tool

@tool("greet", "Greet someone by name.", params={"name": {"type": "string"}}, required=["name"],
      agent="Fun", fast=True, triggers=[r"^say hello to (?P<name>\w+)"])
def greet(ctx, name):
    ctx.step("Composing the greeting")      # appears in the Execution panel
    return f"Hello {name}!"
```

Restart Alpha. The AI can now call it, `triggers` lets a plain phrase run it with no LLM call, `confirm=True` asks yes/no first, and `ctx.svc` gives you `pc`, `memory`, `web`, `whatsapp`, `n8n`. A broken custom file is skipped (shown under Console/`python main.py tools`) and never stops Alpha.

## 7. Migration guide (from the old project folder)

Automatic:

```bat
python main.py migrate C:\path\to\old_alpha_folder
```

* **API keys and settings**: the old `.env` is merged into the new one (your existing values here are kept; `ALPHA_WEB_TOKEN` becomes `SERVER_TOKEN`; obsolete Streamlit settings are skipped).
* **Memory**: `alpha_memory.db` (notes, remembered projects, screen-time history, chat log) is copied to `data\` (an existing database is backed up first).
* **Your custom Python tools**: every top-level function in the old folder's other `.py` files gets a draft wrapper in `tools\custom\migrated_<file>.py.draft`. Nothing is executed during migration. Review the draft (fix the description/parameter types), rename it to `.py`, run `python main.py tools` to confirm.
* **Voice handling**: older versions had no voice profile; run `python main.py enroll`. Old microphone/Whisper settings (`ALPHA_MIC`, `ALPHA_WHISPER_MODEL`, …) carry over via `.env`.

Manual equivalent: copy the keys you need from the old `.env` into the new one, copy `alpha_memory.db` into `data\`, and turn an old function `f(a, b)` into `@tool("f", "…", params={"a": {...}, "b": {...}}) def f(ctx, a, b): return str(old_f(a, b))`.

## 8. Troubleshooting

| Problem | Fix |
|---|---|
| "Voice not recognised" for your own voice | `python main.py voice-test`; raise `ALPHA_VOICE_STRICTNESS` (1.3); re-enroll in your normal room with more variety |
| Alpha ignores everything | Say "Alpha" **first**; check the gate chip and level bar in the dashboard; `python main.py mic-test` |
| Dashboard says *Reconnecting / Server offline* | Alpha isn't running or is restarting; the page recovers by itself. *Session expired*: reload the page |
| "Playwright / browser" errors | `pip install playwright`; Edge or Chrome must be installed (or `python -m playwright install chromium`) |
| WhatsApp can't find the message box | WhatsApp Web changed: update the selector lists in `tools/whatsapp_tool.py` |
| Gemini key / quota errors | Re-copy the key; free-tier limits are per model (Alpha rotates models automatically) |
| Anything else | `data\alpha.log`, or the dashboard's Console tab |

## 9. Security notes

The dashboard can control your PC: it listens on 127.0.0.1 only, requires a random per-run token, checks the Host header (DNS-rebinding), accepts same-origin POSTs only and sends no CORS headers. Dangerous actions (delete, shutdown/restart, closing windows, WhatsApp, n8n workflows) always ask yes/no, enforced in code so the AI cannot skip it. Don't set `ALPHA_WEB_HOST=0.0.0.0` unless you trust the network. Chat text and logs are rendered as plain text (never HTML).
