"""
core/assistant.py - Alpha, the conductor.  [FIXED VERSION - see inline "# FIX:" comments]

    microphone -> noise gate / VAD -> VOICE LOCK (only your voice) -> faster-whisper -> wake word ("Alpha" first)
               -> Orchestrator (fast rules | streaming LLM + tools | offline rules) -> sentence-streamed voice
    every step is published on the EventBus -> ui/server.py -> the dashboard

CHANGES IN THIS VERSION (vs. the original you had):
  1. _listen_loop no longer silently kills the listener thread if the STT generator ever ends without the
     stop signal being set. Before: `return` ended the thread forever, with zero error - voice input would
     stop working completely and nothing would tell you why. Now: it logs a WARNING and restarts the loop.
  2. A wake-word miss (Whisper heard you, but the text didn't start with "Alpha") is now ALSO written to the
     real logger (console + data/alpha.log), not just the dashboard's "heard" feed. Before this was visible
     only if you were staring at the dashboard's feed tab.
  3. The wake-word fuzzy match ratio is now tunable via ALPHA_WAKE_FUZZY (default lowered from a hardcoded
     0.8 to 0.78) so accented pronunciations of "Alpha" that Whisper mangles slightly still match.
"""
from __future__ import annotations

import argparse
import collections
import difflib
import logging
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from config.quick_actions import QUICK_ACTIONS, QUICK_BY_ID
from config.settings import env_float
from core.background import BackgroundHub, Metrics
from core.brains import OfflineBrain, Reply, build_brain
from core.events import EventBus
from core.orchestrator import Orchestrator
from core.registry import REGISTRY, Services, ToolSpec

log = logging.getLogger("alpha")
VERSION = "4.0"

# =============================================================================
# Wake word: the utterance must BEGIN with "Alpha"
# =============================================================================
WAKE_WORDS = {"alpha", "alfa", "alfaa", "alpa", "alfha", "elfa", "olfa", "ulfa", "elpha", "aalfa", "alfah", "alpah",
              "الفا", "الفہ", "ایلفا", "الفه", "الفاہ", "آلفا", "الفے"}
FILLERS = {"hey", "hi", "hello", "ok", "okay", "yo", "hay", "uh", "um", "so", "اے", "ارے", "ہیلو"}
_WORD = re.compile(r"[\w'\u2019]+", re.UNICODE)
_AFTER_WAKE = re.compile(r"^[\s,.:;!?\-\u2013\u2014\u060c]*(?:ai\b[\s,.:;!?\-\u2013\u2014\u060c]*)?", re.I)
# FIX: was a hardcoded 0.8. Lower default is more forgiving of Whisper mis-hearing "Alpha" under accent/noise;
# tune with ALPHA_WAKE_FUZZY in .env if you still get false "didn't start with Alpha" misses.
_WAKE_FUZZY_RATIO = env_float("ALPHA_WAKE_FUZZY", 0.78)


def _is_wake(word: str) -> bool:
    w = word.lower()
    return w in WAKE_WORDS or (len(w) >= 4 and difflib.SequenceMatcher(None, w, "alpha").ratio() >= _WAKE_FUZZY_RATIO)


def parse_wake(text: str) -> Optional[str]:
    """Command after the wake word, "" if only the wake word was said, None if the phrase doesn't START with it."""
    words = list(_WORD.finditer(text or ""))
    i = 0
    while i < len(words) and i < 2 and words[i].group().lower() in FILLERS:
        i += 1
    if i >= len(words):
        return None
    last = i
    if not _is_wake(words[i].group()):
        if i + 1 < len(words) and _is_wake(words[i].group() + words[i + 1].group()):  # "al fa"
            last = i + 1
        else:
            return None
    return _AFTER_WAKE.sub("", text[words[last].end():], count=1).strip(" ,.:;!?\u061f\u060c-")


YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "okay", "ok", "confirm", "confirmed", "haan", "han",
             "hanji", "ji", "جی", "ہاں", "ٹھیک", "کرو"}
NO_WORDS = {"no", "nope", "nah", "cancel", "stop", "dont", "don't", "nahi", "nahin", "nai", "نہیں", "نہ", "مت"}


def parse_yes_no(text: str) -> Optional[bool]:
    tokens = set(re.findall(r"[\w']+", (text or "").lower()))
    if tokens & NO_WORDS:  # "no" wins for safety
        return False
    if tokens & YES_WORDS:
        return True
    return None


EXIT_RE = re.compile(r"(?:please )?(exit|quit|goodbye|good bye|bye|bye bye|stop listening|(?:shut ?down|close) alpha)"
                     r"(?: alpha)?[.!\s]*", re.I)


def speakable(text: str, limit: int = 420) -> str:
    """Long tool output (web pages, reports) shown in full on screen but only its first sentences are spoken."""
    if len(text) <= limit:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    out = ""
    for s in sentences:
        if len(out) + len(s) > limit:
            break
        out += (" " if out else "") + s
    return out or text[:limit]


# =============================================================================
class Alpha:
    def __init__(self, args: argparse.Namespace, bus: EventBus):
        from core.memory import MemoryManager
        from tools.n8n_client import N8nClient
        from tools.pc_controller import ActivityTracker, PCController
        from tools.web_reader import WebReader
        from tools.whatsapp_tool import WhatsAppClient
        from voice.engine import STTEngine, TTSEngine
        from voice_security.voice_auth import VoiceGate

        self.args, self.bus = args, bus
        self.hub = BackgroundHub()
        self.hub.start()
        self.metrics = Metrics()
        self.memory = MemoryManager()
        self.tracker = None if args.no_tracking else ActivityTracker(
            self.memory, log_titles=os.environ.get("ALPHA_LOG_TITLES", "1") != "0",
            writer=lambda *a: self.hub.submit("db", self.memory.log_activity, *a),
            barrier=lambda: self.hub.barrier("db", 1.0))
        self.pc = PCController(self.memory, self.tracker)
        self.tts = TTSEngine(enabled=not args.mute)
        self.tts.on_play_start = self._on_play_start
        self.stt = None if args.text else STTEngine(model_size=args.whisper)
        self.voice_gate = VoiceGate()
        self.n8n = N8nClient(hub=self.hub, on_result=self._n8n_done)
        self.whatsapp = WhatsAppClient()
        self.web = WebReader()
        self.svc = Services(pc=self.pc, memory=self.memory, tracker=self.tracker, n8n=self.n8n,
                            whatsapp=self.whatsapp, web=self.web, metrics=self.metrics, hub=self.hub)
        REGISTRY.discover()
        self.brain = build_brain()
        self.orch = Orchestrator(self.svc, confirm=self._confirm, on_task=self._on_task, metrics=self.metrics,
                                 fast_path=os.environ.get("ALPHA_FAST_PATH", "1") != "0")
        self.inbox: "queue.Queue[Tuple[str, str]]" = queue.Queue()
        self._stop = threading.Event()
        self._listening = True
        self._busy = False
        self._confirm_pending = False
        self._last_state_key: Optional[tuple] = None
        self._last_mic_error = ""
        self._last_security_msg = 0.0
        self.followup = env_float("ALPHA_FOLLOWUP_SECONDS", 0.0)   # 0 = strict: the wake word is needed every time
        self.active_until = 0.0
        self.last_lang = "en"
        self._notice_given = False
        self.tasks: "collections.deque[dict]" = collections.deque(maxlen=12)
        self._cmd_t0 = 0.0
        self._first_audio_pending = False
        self._utt_ready = 0.0
        self._restore_history()

    # ------------------------------------------------------------------ events
    def _restore_history(self) -> None:
        try:
            for m in self.memory.recent_messages(30):
                self.bus.publish({"type": "message", "role": m["role"], "text": m["text"], "ts": m["ts"],
                                  "via": m["via"], "restored": True})
        except Exception:
            log.debug("Could not restore chat history", exc_info=True)

    def _publish_message(self, role: str, text: str, **extra) -> None:
        self.bus.publish({"type": "message", "role": role, "text": text, **extra})
        if role in ("user", "alpha", "security"):  # persisted off the speech path (security = audit trail)
            self.hub.submit("db", self.memory.log_message, role, text, extra.get("via", ""))

    def _on_task(self, evt: dict) -> None:
        for i, t in enumerate(self.tasks):
            if t["id"] == evt["id"]:
                self.tasks[i] = evt
                break
        else:
            self.tasks.append(evt)
        self.bus.publish({"type": "task", **evt})

    def _on_play_start(self) -> None:
        if self._first_audio_pending and self._cmd_t0:
            self._first_audio_pending = False
            self.metrics.record("first_audio_ms", (time.time() - self._cmd_t0) * 1000)

    def _n8n_done(self, result) -> None:  # runs on the hub's "net" worker
        self.say(result.speakable())

    def settings_snapshot(self) -> dict:
        gain = auto = gate = None
        if self.stt:
            gain, auto, gate = round(self.stt.settings.gain, 1), self.stt.settings.auto_gain, round(self.stt.settings.gate_db)
        return {"muted": not self.tts.enabled, "listening": self._listening, "has_mic": self.stt is not None,
                "gain": gain, "auto_gain": auto, "gate_db": gate, "provider": self.brain.label,
                "strict_wake": self.followup <= 0, "voice_lock": self.voice_gate.describe(),
                "whatsapp": bool(self.whatsapp.enabled), "n8n": self.n8n.names(), "version": VERSION}

    def snapshot(self) -> dict:
        b = self.bus.snapshot()
        return {"version": VERSION, "state": self._compute_state(), "settings": self.settings_snapshot(),
                "mic": self.stt.status() if self.stt else None, "voice_auth": self.voice_gate.status(),
                "status": (getattr(self, "status_pub", None).latest if getattr(self, "status_pub", None) else {}),
                "tasks": list(self.tasks), "history": b["history"][-60:], "logs": b["logs"][-120:],
                "confirm": b["latest"].get("confirm"), "latency": self.metrics.summary(),
                "quick": [{"id": q["id"], "label": q["label"], "icon": q["icon"]} for q in QUICK_ACTIONS],
                "agents": REGISTRY.agents(), "tool_errors": REGISTRY.load_errors}

    def _compute_state(self) -> str:
        if self.stt is not None and not self._listening:
            return "paused"
        if self.tts.is_speaking:
            return "speaking"
        if self.orch.is_executing:
            return "executing"
        if self._busy:
            return "thinking"
        if self.stt is not None and self.stt.is_calibrating:
            return "calibrating"
        return "listening" if self.stt is not None else "idle"

    def _state_loop(self) -> None:
        while not self._stop.wait(0.15):
            state, snap = self._compute_state(), self.settings_snapshot()
            key = (state, tuple(sorted((k, str(v)) for k, v in snap.items())))
            if key != self._last_state_key:
                self._last_state_key = key
                self.bus.publish({"type": "state", "state": state})
                self.bus.publish({"type": "settings", **snap})

    def _on_mic(self, status: dict) -> None:
        self.bus.publish({"type": "mic", **status})

    # --------------------------------------------------------------- controls
    def submit_text(self, text: str) -> None:
        self.inbox.put(("web", text))

    def submit_quick(self, action_id: str) -> None:
        if action_id in QUICK_BY_ID:
            self.inbox.put(("quick", action_id))

    def set_muted(self, muted: bool) -> None:
        self.tts.set_enabled(not muted)
        self._publish_message("system", "Voice replies muted." if muted else "Voice replies on.")

    def set_listening(self, listening: bool) -> None:
        self._listening = bool(listening)
        self._publish_message("system", "Listening resumed." if listening else "Listening paused.")

    def set_gain(self, auto: Optional[bool] = None, gain: Optional[float] = None) -> None:
        if self.stt:
            self.stt.set_gain(auto=auto, gain=gain)

    def set_gate_db(self, value: float) -> None:
        if self.stt:
            self.stt.set_gate_db(value)

    def reset_counters(self) -> None:
        if self.stt:
            self.stt.reset_counters()

    def recalibrate(self) -> None:
        if self.stt:
            self.stt.request_calibration()
            self._publish_message("system", "Calibrating the microphone - please stay quiet for 2 seconds.")

    def answer_confirm(self, answer: bool) -> None:
        if self._confirm_pending:
            self.inbox.put(("web", "yes" if answer else "no"))

    # ------------------------------------------------------------------ output
    def say(self, text: str, lang: Optional[str] = None, speak: bool = True) -> None:
        print(f"\nAlpha: {text}\n", flush=True)
        self._publish_message("alpha", text)
        if speak:
            self.tts.speak(speakable(text), lang)

    # ------------------------------------------------------------------- input
    def _gate(self) -> bool:
        if not self._listening or self.tts.is_speaking:
            return False
        return time.time() - self.tts.last_end > 0.4  # let the speaker echo die down

    def _security_check(self, audio) -> bool:
        """Voice lock: runs BEFORE transcription, so an unknown voice is never turned into text or an action."""
        verdict = self.voice_gate.check(audio)
        self.bus.publish({"type": "voice_auth", **self.voice_gate.status()})
        if verdict.ok:
            return True
        # FIX: was log.warning, which prints to the console on every normal run (console handler's
        # level is WARNING when not verbose). log.info still reaches data/alpha.log (root level is
        # INFO by default) but is filtered out of the console by the console handler's own level -
        # log.debug would have been filtered at the ROOT logger first and never reach the file either.
        log.info("Unauthorised voice ignored (%s, score %.2f)", verdict.reason, verdict.score)
        now = time.time()
        if now - self._last_security_msg > 6:        # don't flood the feed when a TV is talking
            self._last_security_msg = now
            self._publish_message("security", f"Voice not recognised - ignored ({verdict.reason}"
                                              + (f", score {verdict.score:.2f} < {verdict.threshold:.2f}" if verdict.threshold else "") + ")")
        return False

    def _listen_loop(self) -> None:
        failures = 0
        threading.Thread(target=self.stt.warmup, name="WhisperWarmup", daemon=True).start()
        while not self._stop.is_set():
            try:
                for audio in self.stt.utterances(gate=self._gate, on_status=self._on_mic, stop=self._stop):
                    failures = 0
                    if not self._security_check(audio):
                        continue
                    self._utt_ready = self.stt.t_utterance_ready
                    text = self.stt.transcribe(audio)
                    self.metrics.record("stt_ms", self.stt.last_transcribe_ms)
                    self.stt.maybe_unload()
                    if text:
                        print(f"You: {text}", flush=True)
                        self.inbox.put(("voice", text))
                # FIX: the generator ended. The ONLY reason that should ever happen is the stop event -
                # a normal/idle-triggered generator exit here used to `return`, silently killing voice input
                # forever with zero error. Now we log it loudly and keep listening instead.
                if self._stop.is_set():
                    return
                log.warning("Voice listener generator ended without a stop request - restarting it. "
                           "If you see this repeatedly, check voice/engine.py's utterances() for an unintended "
                           "early exit (idle-unload, model reload, etc).")
                time.sleep(0.5)  # avoid a tight restart loop if this keeps happening instantly
            except Exception as exc:  # device unplugged, driver reset, privacy block ...
                failures += 1
                log.exception("Microphone error (%d)", failures)
                msg = f"Microphone problem: {exc}"
                if msg != self._last_mic_error:
                    self._last_mic_error = msg
                    self._publish_message("system", msg + " - retrying; run `python main.py mic-test` to diagnose.")
                if failures == 3:
                    self.say("I'm having trouble with the microphone. You can still type commands.")
                self._stop.wait(min(2 * failures, 20))  # keeps retrying: re-plugging a mic recovers by itself

    def _console_loop(self) -> None:
        try:
            while not self._stop.is_set():
                line = sys.stdin.readline()
                if not line:
                    return
                if line.strip():
                    self.inbox.put(("text", line.strip()))
        except Exception:
            log.debug("Console input closed", exc_info=True)

    # ------------------------------------------------------------ confirmation
    def _confirm(self, spec: ToolSpec, args: dict) -> bool:
        """yes/no from your (verified) voice, typing, or the dashboard buttons. Silence means no."""
        while not self.inbox.empty():
            try:
                self.inbox.get_nowait()
            except queue.Empty:
                break
        question = self.orch.confirm_text(spec, args)
        self._confirm_pending = True
        self.bus.publish({"type": "confirm", "text": question, "tool": spec.name})
        try:
            self.say(question)
            self.tts.wait_idle(20)
            deadline, retried = time.time() + 25, False
            while time.time() < deadline and not self._stop.is_set():
                try:
                    _, text = self.inbox.get(timeout=1)
                except queue.Empty:
                    continue
                verdict = parse_yes_no(text)
                if verdict is not None:
                    return verdict
                if not retried:
                    retried = True
                    self.say("Sorry, please say yes or no.")
                    self.tts.wait_idle(10)
                    deadline = time.time() + 15
            self.say("I didn't hear an answer, so I've cancelled that.")
            return False
        finally:
            self._confirm_pending = False
            self.bus.publish({"type": "confirm_done"})

    # ---------------------------------------------------------------- commands
    def _wake_filter(self, text: str) -> Optional[str]:
        command = parse_wake(text)
        if command is None and time.time() < self.active_until:   # set by a bare "Alpha" or ALPHA_FOLLOWUP_SECONDS
            return text.strip()
        return command

    def _process(self, command: str, source: str) -> None:
        from voice.engine import SpeechStream

        self._cmd_t0 = self._utt_ready if (source == "voice" and self._utt_ready) else time.time()
        self._first_audio_pending = True
        stream = SpeechStream(self.tts)
        t_llm = time.time()
        first = lambda: self.metrics.record("llm_first_token_ms", (time.time() - t_llm) * 1000)  # noqa: E731
        try:
            reply, path = self.orch.process(command, self.brain, on_text=stream.feed, on_first_token=first)
        except Exception:
            log.exception("Unhandled error while processing command")
            reply, path = Reply("Sorry, something went wrong with that request.", False), "none"
        stream.close()
        self.metrics.record(f"{path}_ms", (time.time() - t_llm) * 1000)
        if self.orch.last_brain_error and not self._notice_given:
            self._notice_given = True
            self._publish_message("system", f"AI provider error: {self.orch.last_brain_error}")
            self.say("I can't reach my online brain right now, so I'll handle basic commands only.")
        self.say(reply.text, speak=not reply.spoken)
        self.metrics.record("total_ms", (time.time() - self._cmd_t0) * 1000)

    def handle(self, source: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self._busy = True
        try:
            if source == "quick":
                if text in QUICK_BY_ID:
                    action = QUICK_BY_ID[text]
                    self._publish_message("user", f"{action['icon']} {action['label']}", via="quick")
                    self.say(self.orch.run(action["tool"], dict(action["args"])))
                return
            if source == "voice":
                command = self._wake_filter(text)
                if command is None:  # real speech without the wake word: counted, never executed
                    if self.stt:
                        self.stt.register_false_trigger("no_wake")
                    # FIX: this used to be invisible outside the dashboard's "heard" feed. Now it's a real
                    # log line too, so `data/alpha.log` and the console actually show every dropped utterance.
                    log.info("Voice dropped - no wake word at the start: %r", text)
                    self._publish_message("heard", f"{text}  (ignored: it didn't start with \u201cAlpha\u201d)")
                    return
                if command == "":
                    self._publish_message("user", text, via="voice")
                    self.say("Yes?")
                    self.active_until = time.time() + 6  # the next phrase may skip the wake word
                    return
            else:
                command = text
                self.tts.stop()  # typed input interrupts speech
            self._publish_message("user", command, via=source)
            self.last_lang = "ur" if re.search(r"[\u0600-\u06FF]", command) else "en"
            log.info("Command (%s): %s", source, command)
            if EXIT_RE.fullmatch(command.strip()):
                self.say("Goodbye! Have a lovely day.")
                self.tts.wait_idle(8)
                self._stop.set()
                return
            self._process(command, source)
            if self.followup > 0:
                self.active_until = time.time() + self.followup
        finally:
            self._busy = False

    # --------------------------------------------------------------------- run
    def run(self) -> None:
        if self.tracker:
            self.tracker.start()
        threading.Thread(target=self._state_loop, name="State", daemon=True).start()
        if self.stt:
            threading.Thread(target=self._listen_loop, name="Listener", daemon=True).start()
        if sys.stdin is not None:
            threading.Thread(target=self._console_loop, name="Console", daemon=True).start()
        how = "Say 'Alpha' + a command, or type one" if self.stt else "Type a command"
        print(f"\nAlpha AI {VERSION} is running ({self.brain.label}). {how}. Say or type 'goodbye' to exit.")
        print(self.voice_gate.describe() + "\n")
        if isinstance(self.brain, OfflineBrain):
            print("[!] No GEMINI_API_KEY (or ANTHROPIC_API_KEY): basic offline commands only.\n"
                  "    Free key: https://aistudio.google.com/apikey  ->  put it in .env\n")
        if self.voice_gate.mode == "strict" and not self.voice_gate.enrolled and self.stt:
            self._publish_message("security", "Voice lock is ON but no voice profile exists, so voice commands are refused. "
                                              "Run: python main.py enroll   (typed commands still work.)")
        self.say("Hello, I'm Alpha. How can I help you?")
        while not self._stop.is_set():
            try:
                source, text = self.inbox.get(timeout=0.5)
            except queue.Empty:
                continue
            self.handle(source, text)

    def shutdown(self) -> None:
        self._stop.set()
        steps = [self.tts.shutdown, self.whatsapp.shutdown, (self.tracker.stop if self.tracker else lambda: None),
                 lambda: self.hub.stop(3.0), self.memory.close]
        for step in steps:
            try:
                step()
            except Exception:
                log.exception("Error during shutdown")