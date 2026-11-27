"""
voice_engine.py - Alpha AI's voice (v2).

  TTSEngine : edge-tts neural female voices -> pygame.mixer playback on a worker
              thread. Can be muted/unmuted at runtime. Falls back to the built-in
              Windows SAPI voice when offline.

  STTEngine : a microphone pipeline tuned for quiet / noisy built-in laptop mics:

      persistent input stream (opens at 16 kHz, or the device's native rate and
      resamples; mono-mixes multi-channel array mics)
        -> 80 Hz high-pass (kills rumble / DC offset)
        -> ambient-noise calibration + continuously tracked noise floor
        -> dynamic energy threshold with hysteresis (soft word endings are kept)
        -> pre-roll buffering (the first syllable is never clipped)
        -> silence trimming
        -> mild spectral-gate noise reduction (numpy only, adjustable)
        -> auto-gain / peak normalisation so soft speech reaches Whisper loud enough
        -> faster-whisper (int8, CPU), with a language guard for en/ur
           (falls back to Google SpeechRecognition if Whisper cannot load)

  Everything is logged under "alpha.mic" and exposed through STTEngine.status()
  so the dashboard (and `--mic-test`) can show what the microphone is doing.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import math
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, List, Optional

import numpy as np

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

log = logging.getLogger("alpha.voice")
mic_log = logging.getLogger("alpha.mic")
IS_WIN = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

SAMPLE_RATE = 16000          # what Whisper wants
BLOCK = 480                  # 30 ms analysis block at 16 kHz
NFFT = 512

_URDU_CHARS = re.compile(r"[\u0600-\u06FF\u0750-\u077F]")


def detect_lang(text: str) -> str:
    """'ur' if the text is mostly Arabic-script (Urdu), otherwise 'en'."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "en"
    urdu = sum(1 for c in letters if _URDU_CHARS.match(c))
    return "ur" if urdu / len(letters) > 0.3 else "en"


# =============================================================================
# Text-to-speech
# =============================================================================
def split_sentences(text: str, max_len: int = 220) -> List[str]:
    """Split at sentence ends (. ! ? ؟ ۔ and newlines); over-long sentences are cut at commas/spaces."""
    out: List[str] = []
    for part in re.split(r"(?<=[.!?؟۔])\s+|\n+", (text or "").strip()):
        part = part.strip()
        while len(part) > max_len:
            cut = max(part.rfind(",", 0, max_len), part.rfind("\u060c", 0, max_len), part.rfind(" ", 0, max_len))
            if cut < max_len * 0.4:
                cut = max_len
            out.append(part[:cut].strip(" ,\u060c"))
            part = part[cut:].strip(" ,\u060c")
        if part:
            out.append(part)
    return out


class TTSEngine:
    """
    Two-stage speech pipeline so audio starts as early as possible:

        speak()/SpeechStream -> [text queue] -> synthesiser thread (edge-tts, persistent event loop)
                             -> [audio queue, 4 deep] -> player thread (pygame)

    While sentence N plays, sentence N+1 is already being synthesised. stop() flushes both
    queues instantly. Falls back to the Windows SAPI voice when edge-tts is unreachable.
    """

    # Female neural voices, tried in order. (ur-PK-AsadNeural is a male voice, so it is not used.)
    VOICES = {
        "en": ["en-US-AvaNeural", "en-US-JennyNeural"],
        "ur": ["ur-PK-UzmaNeural", "ur-IN-GulNeural"],
    }

    def __init__(self, rate: str = "+0%", volume: str = "+0%", enabled: bool = True):
        self.rate, self.volume = rate, volume
        self.enabled = enabled                      # runtime mute switch (see set_enabled)
        self.last_end = 0.0
        self.last_play_start = 0.0
        self.on_play_start: Optional[Callable[[], None]] = None   # fired when audio really starts
        self._text_q: "queue.Queue" = queue.Queue()
        self._audio_q: "queue.Queue" = queue.Queue(maxsize=4)
        self._lock = threading.Lock()
        self._pending = 0
        self._gen = 0
        self._idle = threading.Event()
        self._idle.set()
        self._threads = [threading.Thread(target=self._synth_worker, name="TTS-synth", daemon=True),
                         threading.Thread(target=self._play_worker, name="TTS-play", daemon=True)]
        for t in self._threads:
            t.start()

    # ------------------------------------------------------------- public API
    @property
    def is_speaking(self) -> bool:
        return self._pending > 0

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        if not self.enabled:
            self.stop()

    def speak(self, text: str, lang: Optional[str] = None, block: bool = False) -> None:
        if not self.enabled or not (text or "").strip():
            return
        chunks = split_sentences(text) or [text.strip()]
        with self._lock:
            for chunk in chunks:
                self._pending += 1
                self._text_q.put((self._gen, chunk, lang))
            self._idle.clear()
        if block:
            self.wait_idle()

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        return self._idle.wait(timeout)

    def stop(self) -> None:
        """Interrupt current speech and drop everything queued."""
        with self._lock:
            self._gen += 1
            drained = 0
            for q in (self._text_q, self._audio_q):
                while True:
                    try:
                        item = q.get_nowait()
                    except queue.Empty:
                        break
                    if item is None:
                        continue
                    drained += 1
                    if q is self._audio_q and item[1]:
                        self._remove(item[1])
            self._pending = max(0, self._pending - drained)
            if self._pending == 0:
                self._idle.set()

    def shutdown(self) -> None:
        self.stop()
        self._text_q.put(None)
        for t in self._threads:
            t.join(timeout=2)
        try:
            import pygame

            if pygame.mixer.get_init():
                pygame.mixer.quit()
        except Exception:
            pass

    # --------------------------------------------------------------- internals
    @staticmethod
    def _remove(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    def _finish(self) -> None:
        with self._lock:
            self._pending = max(0, self._pending - 1)
            self.last_end = time.time()
            if self._pending == 0:
                self._idle.set()

    @staticmethod
    def _clean(text: str) -> str:
        text = re.sub(r"https?://\S+", " link ", text)
        text = re.sub(r"[*_`#>~|]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _synth_worker(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while True:
            item = self._text_q.get()
            if item is None:
                self._audio_q.put(None)
                break
            gen, text, lang = item
            text = self._clean(text)
            if gen != self._gen or not text:
                self._finish()
                continue
            path = None
            try:
                path = self._synthesize(loop, text, lang or detect_lang(text))
            except Exception:
                log.exception("TTS synthesis failure")
            self._audio_q.put((gen, path, text))  # path None -> SAPI fallback in the player
        loop.close()

    def _play_worker(self) -> None:
        while True:
            item = self._audio_q.get()
            if item is None:
                break
            gen, path, text = item
            try:
                if gen == self._gen:
                    self.last_play_start = time.time()
                    callback = self.on_play_start
                    if callback:
                        try:
                            callback()
                        except Exception:
                            log.debug("on_play_start callback failed", exc_info=True)
                    if path:
                        self._play(path, gen)
                    else:
                        log.warning("edge-tts unavailable; using Windows SAPI fallback.")
                        self._speak_sapi(text)
            except Exception:
                log.exception("TTS playback failure")
            finally:
                if path:
                    self._remove(path)
                self._finish()

    def _synthesize(self, loop: "asyncio.AbstractEventLoop", text: str, lang: str) -> Optional[str]:
        for voice in self.VOICES.get(lang, self.VOICES["en"]):
            fd, path = tempfile.mkstemp(suffix=".mp3", prefix="alpha_tts_")
            os.close(fd)
            try:
                loop.run_until_complete(asyncio.wait_for(self._edge_save(text, voice, path), timeout=20))
                if os.path.getsize(path) > 0:
                    return path
            except Exception as exc:
                log.warning("edge-tts voice %s failed: %s", voice, exc)
            self._remove(path)
        return None

    async def _edge_save(self, text: str, voice: str, path: str) -> None:
        import edge_tts

        await edge_tts.Communicate(text, voice, rate=self.rate, volume=self.volume).save(path)

    def _play(self, path: str, gen: int) -> None:
        import pygame

        if not pygame.mixer.get_init():
            pygame.mixer.init()
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            if gen != self._gen:  # stop() was called
                pygame.mixer.music.stop()
                break
            time.sleep(0.03)
        try:
            pygame.mixer.music.unload()  # release the file lock on Windows
        except AttributeError:
            pass

    @staticmethod
    def _speak_sapi(text: str) -> None:
        if not IS_WIN:
            return
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "try { $s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female) } catch {}; "
            "$s.Speak($env:ALPHA_TTS_TEXT)"
        )
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           env={**os.environ, "ALPHA_TTS_TEXT": text}, timeout=60,
                           creationflags=NO_WINDOW, check=False)
        except (subprocess.SubprocessError, OSError):
            log.exception("SAPI fallback failed")


class SpeechStream:
    """
    Feed it streamed LLM text; every completed sentence is handed to the TTS pipeline at once,
    so Alpha starts talking after the first sentence instead of after the whole answer.
    """

    _BOUNDARY = re.compile(r"[.!?؟۔][\"')\]]*(?=\s)|\n")

    def __init__(self, tts: TTSEngine, lang: Optional[str] = None, first_min: int = 14, min_len: int = 28):
        self.tts, self.lang, self.first_min, self.min_len = tts, lang, first_min, min_len
        self._buf = ""
        self._sent = 0
        self.parts: List[str] = []

    def feed(self, delta: str) -> None:
        if delta:
            self._buf += delta
            self._drain(final=False)

    def close(self) -> str:
        self._drain(final=True)
        return self.text

    @property
    def text(self) -> str:
        return " ".join(self.parts)

    def _drain(self, final: bool) -> None:
        while True:
            need = self.first_min if self._sent == 0 else self.min_len
            cut = next((m.end() for m in self._BOUNDARY.finditer(self._buf) if m.end() >= need), None)
            if cut is None:
                break
            self._emit(self._buf[:cut])
            self._buf = self._buf[cut:]
        if final and self._buf.strip():
            self._emit(self._buf)
            self._buf = ""

    def _emit(self, sentence: str) -> None:
        sentence = sentence.strip()
        if sentence:
            self._sent += 1
            self.parts.append(sentence)
            self.tts.speak(sentence, self.lang)


# =============================================================================
# Audio DSP helpers (numpy only)
# =============================================================================
def db(x: float) -> float:
    """Linear amplitude -> dBFS (floored at -180)."""
    return 20.0 * math.log10(max(float(x), 1e-9))


def rms_of(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class HighPass:
    """One-pole 80 Hz high-pass filter with state carried across blocks."""

    def __init__(self, fc: float = 80.0, fs: int = SAMPLE_RATE):
        self.a = math.exp(-2.0 * math.pi * fc / fs)
        self.px = 0.0
        self.py = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        a, px, py = self.a, self.px, self.py
        out = np.empty(len(x), dtype=np.float32)
        for i, v in enumerate(x.tolist()):
            py = a * (py + v - px)
            px = v
            out[i] = py
        self.px, self.py = px, py
        return out


class Resampler:
    """Streaming resampler to 16 kHz (exact averaging for integer ratios, else linear)."""

    def __init__(self, src_rate: int, dst_rate: int = SAMPLE_RATE):
        self.src, self.dst = int(src_rate), int(dst_rate)
        self.passthrough = self.src == self.dst
        self.ratio = self.src / self.dst
        self.int_factor = (not self.passthrough) and self.src % self.dst == 0
        self.tail = np.zeros(0, dtype=np.float32)
        self.pos = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        if self.passthrough:
            return x
        buf = np.concatenate([self.tail, x])
        if self.int_factor:
            k = self.src // self.dst
            n = (len(buf) // k) * k
            self.tail = buf[n:]
            return buf[:n].reshape(-1, k).mean(axis=1).astype(np.float32) if n else np.zeros(0, np.float32)
        if len(buf) < 2:
            self.tail = buf
            return np.zeros(0, np.float32)
        n_out = int(math.floor((len(buf) - 1 - self.pos) / self.ratio)) + 1
        if n_out <= 0:
            self.tail = buf
            return np.zeros(0, np.float32)
        idx = self.pos + np.arange(n_out) * self.ratio
        out = np.interp(idx, np.arange(len(buf)), buf).astype(np.float32)
        nxt = idx[-1] + self.ratio
        cut = int(nxt)
        if cut > len(buf):
            self.tail, self.pos = np.zeros(0, np.float32), nxt - len(buf)
        else:
            self.tail, self.pos = buf[cut:], nxt - cut
        return out


def block_spectrum(block: np.ndarray) -> np.ndarray:
    """Magnitude spectrum of one analysis block (used for the noise profile)."""
    return np.abs(np.fft.rfft(block * np.hanning(len(block)).astype(np.float32), n=NFFT)).astype(np.float32)


def spectral_gate(audio: np.ndarray, noise_mag: np.ndarray, strength: float) -> np.ndarray:
    """
    Mild spectral-gating noise reduction. strength 0..1:
      0 = off, 0.5 = moderate (default), 1 = strong (can start to colour speech).
    """
    if strength <= 0 or noise_mag is None or len(audio) < NFFT:
        return audio
    hop = NFFT // 2
    oversub = 1.0 + strength
    floor = max(0.05, 1.0 - 0.85 * strength)
    win = np.hanning(NFFT).astype(np.float32)
    pad = (-(len(audio) - NFFT)) % hop
    x = np.concatenate([audio, np.zeros(pad, np.float32)]).astype(np.float32)
    frames = 1 + (len(x) - NFFT) // hop
    out = np.zeros(len(x), np.float32)
    norm = np.zeros(len(x), np.float32)
    prev_mask = None
    for i in range(frames):
        s = i * hop
        spec = np.fft.rfft(x[s:s + NFFT] * win)
        mag = np.abs(spec)
        mask = np.clip(1.0 - oversub * noise_mag / (mag + 1e-9), floor, 1.0)
        if prev_mask is not None:
            mask = 0.6 * mask + 0.4 * prev_mask  # temporal smoothing -> less "musical noise"
        prev_mask = mask
        out[s:s + NFFT] += np.fft.irfft(spec * mask, n=NFFT).astype(np.float32) * win
        norm[s:s + NFFT] += win * win
    out /= np.maximum(norm, 1e-3)
    return out[:len(audio)]


# =============================================================================
# Utterance detection
# =============================================================================
@dataclass
class MicSettings:
    gain: float = 3.0           # live gain applied for detection (and fixed gain when auto is off)
    auto_gain: bool = True      # adapt gain to how loudly you speak
    min_rms: float = 0.006      # absolute floor of the energy threshold (in gained units)
    vad_ratio: float = 2.5      # threshold = noise_floor * ratio + margin
    vad_margin: float = 0.003
    silence_limit: float = 0.6  # seconds of silence that end an utterance (lower = faster replies)
    gate_db: float = -55.0      # absolute gate on RAW input (dBFS): quieter audio never reaches STT
    speech_check: bool = True   # drop thumps / hiss / key clicks that are not speech-shaped
    max_seconds: float = 15.0
    min_speech: float = 0.25    # shorter bursts are discarded as clicks / bumps
    denoise: float = 0.5        # 0 = off .. 1 = strong
    target_rms: float = 0.07    # speech level fed to Whisper (about -23 dBFS)
    max_gain: float = 30.0


class UtteranceDetector:
    """
    Energy-based voice-activity state machine working on raw 30 ms blocks.
    Levels are expressed in "gained" units (raw RMS x live gain) so the threshold
    scales with the sensitivity setting.
    """

    def __init__(self, settings: MicSettings):
        self.s = settings
        self.noise_floor = 0.01
        self.noise_mag: Optional[np.ndarray] = None
        self.reset()

    def reset(self) -> None:
        self.speaking = False
        self.loud_run = 0
        self.silence = 0.0
        self.voiced = 0.0
        self.samples = 0
        self.chunks: List[np.ndarray] = []
        self.preroll: "collections.deque[np.ndarray]" = collections.deque(maxlen=14)  # ~0.4 s

    @property
    def threshold(self) -> float:
        gate_floor = (10.0 ** (self.s.gate_db / 20.0)) * self.s.gain  # absolute dBFS gate, in gained units
        return min(0.25, max(self.s.min_rms, gate_floor, self.noise_floor * self.s.vad_ratio + self.s.vad_margin))

    def rescale_for_gain(self, old_gain: float, new_gain: float) -> None:
        if old_gain > 0:
            self.noise_floor *= new_gain / old_gain

    def feed(self, raw: np.ndarray) -> Optional[np.ndarray]:
        """Feed one raw block; returns the complete raw utterance when one ends."""
        level = rms_of(raw) * self.s.gain
        thr = self.threshold
        if not self.speaking:
            self.preroll.append(raw)
            if level > thr:
                self.loud_run += 1
                if self.loud_run >= 3:  # ~90 ms of sustained energy
                    self.speaking = True
                    self.chunks = list(self.preroll)
                    self.samples = sum(len(c) for c in self.chunks)
                    self.silence, self.voiced = 0.0, 0.09
            else:
                self.loud_run = 0
                self._track_noise(raw, level)
            return None

        self.chunks.append(raw)
        self.samples += len(raw)
        dt = len(raw) / SAMPLE_RATE
        if level > thr * 0.6:        # hysteresis: tails of words count as speech
            self.silence = 0.0
            self.voiced += dt
        else:
            self.silence += dt
        if self.silence >= self.s.silence_limit or self.samples > self.s.max_seconds * SAMPLE_RATE:
            audio = np.concatenate(self.chunks)
            voiced = self.voiced
            self.reset()
            return audio if voiced >= self.s.min_speech else None
        return None

    def _track_noise(self, raw: np.ndarray, level: float) -> None:
        self.noise_floor = 0.95 * self.noise_floor + 0.05 * level
        if self.s.denoise > 0:
            mag = block_spectrum(raw)
            self.noise_mag = mag if self.noise_mag is None else 0.95 * self.noise_mag + 0.05 * mag


# =============================================================================
# Speech-to-text engine
# =============================================================================
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class STTEngine:
    _HALLUCINATIONS = {
        "you", "thank you", "thanks for watching", "thank you for watching",
        "subtitles by the amara.org community",
    }

    def __init__(self, model_size: Optional[str] = None, language: Optional[str] = None,
                 idle_unload: float = 600.0):
        env = os.environ.get
        self.model_size = model_size or env("ALPHA_WHISPER_MODEL", "base")
        lang = (language or env("ALPHA_STT_LANG", "")).strip().lower()
        self.language = None if lang in ("", "auto") else lang
        langs = env("ALPHA_STT_LANGS", "en,ur")  # restrict auto-detection to these languages
        self.allowed_langs = ([x.strip().lower() for x in langs.split(",") if x.strip()]
                              if self.language is None else [])
        mic = env("ALPHA_MIC", "").strip()
        self.device = int(mic) if mic.isdigit() else (mic or None)
        self.idle_unload = _env_float("ALPHA_STT_IDLE_UNLOAD", idle_unload)  # 0 = keep the model loaded
        self.initial_calibration_s = 1.5
        self.whisper_vad = env("ALPHA_WHISPER_VAD", "0") == "1"   # our own VAD already trimmed the audio
        # FIX: new - rejects a transcription when Whisper itself is not confident there was speech at
        # all. Lower = stricter (rejects more borderline audio); 0.6 is a reasonable starting point.
        self.no_speech_threshold = _env_float("ALPHA_NO_SPEECH_THRESHOLD", 0.6)
        self.auto_gate = env("ALPHA_AUTO_GATE", "1") != "0"
        self.ft_limit = int(_env_float("ALPHA_FALSE_TRIGGER_LIMIT", 6))
        self.ft_window = _env_float("ALPHA_FALSE_TRIGGER_WINDOW", 90.0)
        self._ft_times: "collections.deque[float]" = collections.deque()

        gain_env = env("ALPHA_MIC_GAIN", "auto").strip().lower()
        auto = gain_env in ("", "auto")
        self.settings = MicSettings(
            gain=clamp(3.0 if auto else _env_float("ALPHA_MIC_GAIN", 3.0), 0.5, 40.0),
            auto_gain=auto,
            min_rms=_env_float("ALPHA_MIN_RMS", 0.006),
            vad_ratio=_env_float("ALPHA_VAD_RATIO", 2.5),
            silence_limit=_env_float("ALPHA_SILENCE", 0.6),
            gate_db=clamp(_env_float("ALPHA_GATE_DB", -55.0), -75.0, -20.0),
            speech_check=env("ALPHA_SPEECH_CHECK", "1") != "0",
            denoise=clamp(_env_float("ALPHA_DENOISE", 0.5), 0.0, 1.0),
        )
        self.detector = UtteranceDetector(self.settings)

        # model
        self._model = None
        self._model_lock = threading.Lock()
        self._retry_load_at = 0.0
        self._last_used = time.time()

        # stream / diagnostics
        self._q: "queue.Queue" = queue.Queue(maxsize=900)  # ~27 s of audio at 30 ms
        self._stream = None
        self.device_name = "not opened"
        self.native_rate = SAMPLE_RATE
        self.channels = 1
        self._calib_request: Optional[float] = None
        self._calib: Optional[dict] = None
        self._rms_raw = 0.0
        self._peak_recent = 0.0
        self._gate_open = True
        self._last_level = 0.0
        self._last_emit = 0.0
        self._last_log = time.time()
        self._last_silent_warn = 0.0
        self._clip_blocks = 0
        self._last_data = time.time()
        self.stats = {"utterances": 0, "session_peak": 0.0, "rejected_gate": 0, "rejected_empty": 0,
                      "no_wake": 0, "gate_raises": 0}
        self.t_utterance_ready = 0.0
        self.last_transcribe_ms = 0

    # ================================================================ model
    def _load_model(self):
        with self._model_lock:
            if self._model is not None or time.time() < self._retry_load_at:
                return self._model
            try:
                from faster_whisper import WhisperModel

                log.info("Loading faster-whisper '%s' (int8, CPU)...", self.model_size)
                self._model = WhisperModel(
                    self.model_size, device="cpu", compute_type="int8",
                    cpu_threads=min(4, os.cpu_count() or 2), num_workers=1,
                )
            except Exception:
                log.exception("faster-whisper could not load; will retry in 5 min. Using fallback STT.")
                self._model = None
                self._retry_load_at = time.time() + 300
            return self._model

    def maybe_unload(self) -> None:
        """Free the model's RAM after a few idle minutes (reloads on next use)."""
        if self.idle_unload > 0 and self._model is not None and time.time() - self._last_used > self.idle_unload:
            with self._model_lock:
                self._model = None
            import gc

            gc.collect()
            log.info("Whisper model unloaded after idle.")

    # ============================================================ controls
    @property
    def is_calibrating(self) -> bool:
        return self._calib is not None or self._calib_request is not None

    def request_calibration(self, seconds: float = 1.5) -> None:
        """Re-measure ambient noise (the user should stay quiet for ~2 s)."""
        self._calib_request = seconds

    def set_gain(self, auto: Optional[bool] = None, gain: Optional[float] = None) -> None:
        s = self.settings
        if gain is not None:
            new = clamp(float(gain), 0.5, 40.0)
            self.detector.rescale_for_gain(s.gain, new)
            s.gain = new
        if auto is not None:
            s.auto_gain = bool(auto)
        mic_log.info("Mic gain set: %.1fx (%s)", s.gain, "auto" if s.auto_gain else "manual")

    def set_gate_db(self, value: float) -> None:
        self.settings.gate_db = clamp(float(value), -75.0, -20.0)
        mic_log.info("Noise gate set to %.0f dBFS (raw input)", self.settings.gate_db)

    def reset_counters(self) -> None:
        for key in ("rejected_gate", "rejected_empty", "no_wake", "gate_raises"):
            self.stats[key] = 0
        self._ft_times.clear()

    @property
    def false_triggers(self) -> int:
        return self.stats["rejected_gate"] + self.stats["rejected_empty"] + self.stats["no_wake"]

    def _gate_state(self) -> str:
        det = self.detector
        if self.is_calibrating:
            return "calibrating"
        if not self._gate_open:
            return "blocked"
        if det.speaking or self._rms_raw * self.settings.gain > det.threshold:
            return "open"
        return "closed"

    def status(self) -> Dict:
        s, det = self.settings, self.detector
        g = max(s.gain, 1e-6)
        return {
            "device": self.device_name,
            "rate": self.native_rate,
            "level_db": round(db(self._rms_raw), 1),
            "gained_db": round(db(self._rms_raw * g), 1),
            "noise_db": round(db(det.noise_floor / g), 1),
            "thr_db": round(db(det.threshold / g), 1),
            "gate_db": round(s.gate_db, 1),
            "gate": self._gate_state(),
            "gain": round(g, 2),
            "auto_gain": s.auto_gain,
            "speaking": det.speaking,
            "calibrating": self.is_calibrating,
            "gated": not self._gate_open,
            "signal": self._peak_recent > 3e-4,
            "utterances": self.stats["utterances"],
            "false_triggers": self.false_triggers,
            "rejected_gate": self.stats["rejected_gate"],
            "rejected_empty": self.stats["rejected_empty"],
            "no_wake": self.stats["no_wake"],
            "gate_raises": self.stats["gate_raises"],
        }

    def warmup(self) -> None:
        """Load Whisper and run one dummy pass so the first real command is not slow."""
        model = self._load_model()
        if model is None:
            return
        try:
            noise = (np.random.default_rng(0).standard_normal(SAMPLE_RATE) * 0.01).astype(np.float32)
            segments, _ = model.transcribe(noise, language=self.language or "en", beam_size=1,
                                           condition_on_previous_text=False)
            list(segments)
            mic_log.info("Whisper '%s' warmed up.", self.model_size)
        except Exception:
            log.debug("Whisper warm-up failed", exc_info=True)
        self._last_used = time.time()

    # ============================================================== stream
    def _on_audio(self, indata, frames, time_info, status) -> None:  # PortAudio thread
        if status:
            mic_log.debug("PortAudio status: %s", status)
        try:
            self._q.put_nowait(indata.copy())
        except queue.Full:
            try:
                self._q.get_nowait()
                self._q.put_nowait(indata.copy())
            except (queue.Empty, queue.Full):
                pass

    def _open_stream(self) -> None:
        import sounddevice as sd

        info = sd.query_devices(self.device, "input")
        try:
            host = sd.query_hostapis(info["hostapi"])["name"]
        except Exception:
            host = "?"
        native = int(info.get("default_samplerate") or 48000)
        max_ch = int(info.get("max_input_channels") or 1)
        attempts = []
        for cfg in ((SAMPLE_RATE, 1), (native, 1), (native, max(1, min(max_ch, 2)))):
            if cfg not in attempts:
                attempts.append(cfg)

        last_exc: Optional[Exception] = None
        for rate, ch in attempts:
            try:
                stream = sd.InputStream(device=self.device, samplerate=rate, channels=ch, dtype="float32",
                                        blocksize=int(rate * 0.03), callback=self._on_audio)
                stream.start()
                self._stream, self.native_rate, self.channels = stream, rate, ch
                self.device_name = info["name"]
                mic_log.info("Microphone: '%s' [%s]; opened %d Hz x %d ch (device native %d Hz)",
                             info["name"], host, rate, ch, native)
                return
            except Exception as exc:  # try the next configuration
                last_exc = exc
                mic_log.warning("Could not open mic at %d Hz x %d ch: %s", rate, ch, exc)
        raise RuntimeError(f"Unable to open the microphone '{info['name']}': {last_exc}")

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    # ============================================================ main loop
    def utterances(self, gate: Optional[Callable[[], bool]] = None,
                   on_status: Optional[Callable[[Dict], None]] = None,
                   stop: Optional[threading.Event] = None) -> Iterator[np.ndarray]:
        """
        Generator yielding one cleaned, trimmed, gain-normalised float32 utterance at a time.
        `gate()` returning False means "ignore the mic" (Alpha is speaking / paused).
        The generator keeps the stream open, so levels reach the UI even when gated.
        """
        self._open_stream()
        hp = HighPass()
        rs = Resampler(self.native_rate)
        buf = np.zeros(0, dtype=np.float32)
        self._last_data = time.time()
        self._calib_request = self.initial_calibration_s
        try:
            while not (stop is not None and stop.is_set()):
                try:
                    data = self._q.get(timeout=0.5)
                except queue.Empty:
                    if time.time() - self._last_data > 3.0:
                        raise RuntimeError("No audio from the microphone for 3 s "
                                           "(unplugged, disabled or blocked by Windows privacy settings?)")
                    continue
                self._last_data = time.time()
                mono = data.mean(axis=1) if data.ndim == 2 else data
                buf = np.concatenate([buf, hp.process(rs.process(mono))])
                while len(buf) >= BLOCK:
                    block, buf = buf[:BLOCK], buf[BLOCK:]
                    open_ = True if gate is None else bool(gate())
                    raw_utt = self._process_block(block, open_, on_status)
                    if raw_utt is not None:
                        audio = self._prepare(raw_utt)
                        if audio is not None:
                            self.t_utterance_ready = time.time()
                            yield audio
        finally:
            self._close_stream()

    def _process_block(self, block: np.ndarray, gate_open: bool,
                       on_status: Optional[Callable[[Dict], None]]) -> Optional[np.ndarray]:
        det = self.detector
        rms = rms_of(block)
        peak = float(np.max(np.abs(block))) if len(block) else 0.0
        self._rms_raw = rms
        self._gate_open = gate_open
        self._peak_recent = max(peak, self._peak_recent * 0.995)
        self.stats["session_peak"] = max(self.stats["session_peak"], peak)
        if peak >= 0.98:
            self._clip_blocks += 1

        if self._calib_request is not None and self._calib is None:
            self._calib = {"need": max(10, int(self._calib_request * SAMPLE_RATE / BLOCK)),
                           "levels": [], "mags": []}
            self._calib_request = None
            det.reset()
            mic_log.info("Calibrating ambient noise for %.1f s - stay quiet.",
                         self._calib["need"] * BLOCK / SAMPLE_RATE)

        result = None
        if self._calib is not None:
            self._calib["levels"].append(rms)
            self._calib["mags"].append(block_spectrum(block))
            if len(self._calib["levels"]) >= self._calib["need"]:
                self._finish_calibration()
        elif not gate_open:
            det.reset()
        else:
            result = det.feed(block)

        self._periodic(on_status)
        return result

    def _finish_calibration(self) -> None:
        c, self._calib = self._calib, None
        det, s = self.detector, self.settings
        levels = np.array(c["levels"])
        med = float(np.median(levels))
        keep = [m for lv, m in zip(c["levels"], c["mags"]) if lv <= med * 1.5 + 1e-9]
        det.noise_floor = clamp(med * s.gain, 1e-5, 0.1)
        if keep and s.denoise > 0:
            det.noise_mag = np.mean(keep, axis=0)
        mic_log.info("Calibrated: ambient %.1f dBFS (raw); speech threshold now %.1f dBFS (raw), gain %.1fx",
                     db(med), db(det.threshold / s.gain), s.gain)
        if med < 1e-4:
            mic_log.warning("Ambient level is extremely low (%.1f dBFS). If the dashboard meter never moves when "
                            "you speak, the mic is muted, the wrong device is selected (ALPHA_MIC), or Windows "
                            "blocks desktop apps from using the microphone.", db(med))

    def _periodic(self, on_status: Optional[Callable[[Dict], None]]) -> None:
        now = time.time()
        if on_status is not None and now - self._last_emit >= 0.08:
            self._last_emit = now
            try:
                on_status(self.status())
            except Exception:
                mic_log.debug("on_status callback failed", exc_info=True)
        if now - self._last_log >= 20.0:
            st = self.status()
            mic_log.info("Mic: level %.1f dBFS (recent peak %.1f) | noise %.1f | threshold %.1f | gain %.1fx%s | "
                         "heard %d, rejected %d", st["level_db"], db(self._peak_recent), st["noise_db"],
                         st["thr_db"], st["gain"], " (auto)" if st["auto_gain"] else "",
                         st["utterances"], st["false_triggers"])
            if self._clip_blocks >= 5:
                mic_log.warning("Input is clipping (%d blocks). Lower the Windows mic level/boost or the gain.",
                                self._clip_blocks)
            self._clip_blocks = 0
            self._last_log = now
        if self._peak_recent <= 3e-4 and now - self._last_silent_warn > 60 and not self.is_calibrating:
            self._last_silent_warn = now
            mic_log.warning("The microphone signal is essentially silent (peak %.1f dBFS). Check: mic muted? "
                            "correct device (python main.py list-mics)? Windows Settings > Privacy & "
                            "security > Microphone > 'Let desktop apps access your microphone'.",
                            db(self._peak_recent))

    # ======================================================== post-processing
    def _prepare(self, raw: np.ndarray) -> Optional[np.ndarray]:
        """Trim silence, denoise, and normalise level for Whisper."""
        s, det = self.settings, self.detector
        g = s.gain
        frames = len(raw) // BLOCK
        if frames == 0:
            return None
        levels = np.sqrt(np.mean(raw[:frames * BLOCK].reshape(frames, BLOCK) ** 2, axis=1)) * g
        thr = det.threshold
        active = np.where(levels > thr * 0.6)[0]
        if active.size == 0:
            return None
        pad = int(0.25 * SAMPLE_RATE / BLOCK) + 1
        a, b = max(0, int(active[0]) - pad), min(frames, int(active[-1]) + pad + 1)
        audio = raw[a * BLOCK:b * BLOCK]

        loud = levels[levels > thr]
        speech_rms_raw = float(np.sqrt(np.mean((loud / g) ** 2))) if loud.size else rms_of(audio)
        speech_rms_raw = max(speech_rms_raw, 1e-6)

        if s.speech_check:
            ok, feats = self._looks_like_speech(audio)
            if not ok:
                mic_log.info("Dropped a non-speech sound (%s)", feats)
                self.register_false_trigger("gate")
                return None
        if s.denoise > 0 and det.noise_mag is not None:
            audio = spectral_gate(audio, det.noise_mag, s.denoise)

        if s.auto_gain:
            apply = clamp(s.target_rms / speech_rms_raw, 1.0, s.max_gain)
            new_live = clamp(0.7 * g + 0.3 * apply, 1.0, s.max_gain)  # drift the live gain toward your voice
            det.rescale_for_gain(g, new_live)
            s.gain = new_live
        else:
            apply = g
        out = audio * apply
        peak = float(np.max(np.abs(out))) if len(out) else 0.0
        if peak > 0.95:
            out = out * (0.95 / peak)
        self._last_level = speech_rms_raw * g
        mic_log.info("Utterance %.1fs | speech %.1f dBFS raw | applied gain %.1fx | live gain now %.1fx",
                     len(out) / SAMPLE_RATE, db(speech_rms_raw), apply, s.gain)
        return out.astype(np.float32)

    @staticmethod
    def _looks_like_speech(audio: np.ndarray):
        """
        Cheap shape test on the long-term spectrum: speech keeps most energy in 250-3800 Hz and
        is far from flat. Low-frequency thumps and rumble fail the first test, hiss and key
        clatter fail the second. Deliberately lenient: it only rejects obvious non-speech.
        """
        n_fft, hop = 512, 256
        if len(audio) < n_fft * 2:
            return True, "too short to judge"
        win = np.hanning(n_fft).astype(np.float32)
        spec, count = np.zeros(n_fft // 2 + 1), 0
        for i in range(0, len(audio) - n_fft + 1, hop):
            spec += np.abs(np.fft.rfft(audio[i:i + n_fft] * win)) ** 2
            count += 1
        spec /= max(count, 1)
        freqs = np.fft.rfftfreq(n_fft, 1.0 / SAMPLE_RATE)
        ratio = float(spec[(freqs >= 250) & (freqs <= 3800)].sum() / (spec[1:].sum() + 1e-12))
        band = spec[(freqs >= 100) & (freqs <= 4000)] + 1e-12
        flat = float(np.exp(np.mean(np.log(band))) / np.mean(band))
        return (ratio >= 0.25 and flat <= 0.65), f"voice-band {ratio:.2f}, flatness {flat:.2f}"

    def register_false_trigger(self, kind: str) -> None:
        """
        kind: 'gate'    sound that was not speech / not loud enough
              'empty'   Whisper heard nothing intelligible
              'no_wake' real speech that did not start with the wake word (counted by the orchestrator)
        Repeated gate/empty triggers automatically raise the noise gate by 3 dB (ALPHA_AUTO_GATE).
        """
        self.stats[{"gate": "rejected_gate", "empty": "rejected_empty", "no_wake": "no_wake"}[kind]] += 1
        if kind == "no_wake":
            return
        det = self.detector
        if self._last_level < det.threshold * 2.0:
            det.noise_floor = min(0.05, det.noise_floor * 1.25 + 1e-4)  # decays on its own
        if not self.auto_gate:
            return
        now = time.time()
        self._ft_times.append(now)
        while self._ft_times and now - self._ft_times[0] > self.ft_window:
            self._ft_times.popleft()
        if len(self._ft_times) >= self.ft_limit and self.settings.gate_db < -30.0:
            self._ft_times.clear()
            self.settings.gate_db = min(-30.0, self.settings.gate_db + 3.0)
            self.stats["gate_raises"] += 1
            mic_log.warning("%d false triggers within %.0f s: noise gate raised to %.0f dBFS "
                            "(adjust with the dashboard slider or ALPHA_GATE_DB).",
                            self.ft_limit, self.ft_window, self.settings.gate_db)

    # ========================================================= transcription
    def transcribe(self, audio: np.ndarray) -> str:
        t0 = time.perf_counter()
        self._last_used = time.time()
        model = self._load_model()
        text, used_whisper = "", False
        if model is not None:
            try:
                text, lang = self._whisper(model, audio)
                used_whisper = True
                mic_log.info("Whisper (%s) heard: %r", lang, text)
            except Exception:
                log.exception("Whisper transcription failed; trying fallback recognizer.")
        if not used_whisper:
            text = self._transcribe_fallback(audio)
        text = self._filter(text)
        self.last_transcribe_ms = int((time.perf_counter() - t0) * 1000)
        if text:
            self.stats["utterances"] += 1
        else:
            self.register_false_trigger("empty")
        return text

    def _run_whisper(self, model, audio: np.ndarray, language: Optional[str]):
        options = {"vad_filter": True,
                   "vad_parameters": {"threshold": 0.35, "min_silence_duration_ms": 500, "speech_pad_ms": 300}} \
            if self.whisper_vad else {"vad_filter": False}
        segments, info = model.transcribe(audio, language=language, beam_size=1,
                                          condition_on_previous_text=False, **options)
        # FIX: materialise the segments so we can look at Whisper's own confidence that this audio
        # contains speech AT ALL (no_speech_prob). Without this, forcing a transcription into a
        # fixed language (see _whisper() below) makes Whisper hallucinate fluent-sounding nonsense
        # out of ambient noise/silence instead of returning nothing - exactly the garbled Urdu
        # sentences you saw for audio that was never real speech.
        segments = list(segments)
        if segments:
            avg_no_speech = sum(getattr(s, "no_speech_prob", 0.0) for s in segments) / len(segments)
            if avg_no_speech > self.no_speech_threshold:
                mic_log.info("Discarded a transcription as likely hallucination (no_speech_prob %.2f > %.2f)",
                            avg_no_speech, self.no_speech_threshold)
                return "", info
        return " ".join(seg.text.strip() for seg in segments).strip(), info

    def _whisper(self, model, audio: np.ndarray):
        text, info = self._run_whisper(model, audio, self.language)
        lang = self.language or getattr(info, "language", "?")
        # Auto-detect can pick odd languages on short, soft clips: re-run in the best allowed one.
        if self.language is None and self.allowed_langs and lang not in self.allowed_langs:
            probs = dict(getattr(info, "all_language_probs", None) or [])
            best = max(self.allowed_langs, key=lambda code: probs.get(code, 0.0))
            mic_log.info("Detected '%s' (not in %s) - re-running as '%s'", lang, self.allowed_langs, best)
            text, info = self._run_whisper(model, audio, best)
            lang = best
        return text, lang

    def _transcribe_fallback(self, audio: np.ndarray) -> str:
        try:
            import speech_recognition as sr
        except ImportError:
            return ""
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        data = sr.AudioData(pcm, SAMPLE_RATE, 2)
        lang = {"ur": "ur-PK", "en": "en-US"}.get(self.language or "", "en-US")
        try:
            return sr.Recognizer().recognize_google(data, language=lang)
        except sr.UnknownValueError:
            return ""
        except Exception:
            log.exception("Fallback recognizer failed")
            return ""

    def _filter(self, text: str) -> str:
        text = (text or "").strip()
        bare = re.sub(r"[^\w\s]", "", text.lower()).strip()
        if len(bare) < 2 or bare in self._HALLUCINATIONS:
            return ""
        return text


# =============================================================================
# Command-line microphone test:  python main.py mic-test
# =============================================================================
def run_mic_test(seconds: float = 12.0, model_size: Optional[str] = None) -> None:
    stt = STTEngine(model_size=model_size)
    stop = threading.Event()
    lo, hi, width = -70.0, 0.0, 44

    def pos(v: float) -> int:
        return int(clamp((v - lo) / (hi - lo), 0, 1) * (width - 1))

    def show(st: Dict) -> None:
        cells = ["."] * width
        for i in range(pos(st["level_db"]) + 1):
            cells[i] = "#"
        cells[pos(st["thr_db"])] = "|"
        tag = "CALIBRATING (stay quiet)" if st["calibrating"] else ("SPEECH" if st["speaking"] else "idle")
        print(f"\r[{''.join(cells)}] {st['level_db']:6.1f} dBFS  gain {st['gain']:4.1f}x  {tag:<26}",
              end="", flush=True)

    print(f"\nMicrophone test for {seconds:.0f} s. First stay quiet for ~2 s (calibration), then speak "
          "normally and quietly.\n'|' marks the speech-detection threshold; '#' is your live input level.\n")
    threading.Timer(seconds, stop.set).start()
    try:
        for audio in stt.utterances(gate=lambda: True, on_status=show, stop=stop):
            print(f"\n  Captured {len(audio) / SAMPLE_RATE:.1f}s of speech - transcribing...")
            text = stt.transcribe(audio)
            print(f'  Whisper heard: "{text}"' if text else "  (nothing intelligible recognised)")
    except Exception as exc:
        print(f"\n\nCould not use the microphone: {exc}")
        print("Run `python main.py list-mics` and set ALPHA_MIC=<index> in your .env.")
        return
    finally:
        stop.set()

    st = stt.status()
    peak = db(stt.stats["session_peak"])
    print("\n\n--- Summary ---")
    print(f"Device        : {st['device']} ({st['rate']} Hz)")
    print(f"Peak input    : {peak:.1f} dBFS   Ambient noise: {st['noise_db']:.1f} dBFS   "
          f"Threshold: {st['thr_db']:.1f} dBFS")
    print(f"Live gain     : {st['gain']:.1f}x ({'auto' if st['auto_gain'] else 'manual'})   "
          f"Phrases heard: {st['utterances']}   Rejected: {st['false_triggers']}")
    if peak < -60:
        print("=> Almost no signal. Mic muted, wrong device (--list-mics + ALPHA_MIC), or Windows privacy "
              "settings are blocking desktop apps from the microphone.")
    elif peak < -40:
        print("=> Very quiet input. Auto-gain will compensate, but also raise 'Input volume' in Windows "
              "Sound settings and speak closer to the mic.")
    elif st["noise_db"] > -45:
        print("=> The room/fan noise is high. Try ALPHA_DENOISE=0.8, a headset, or a quieter spot.")
    else:
        print("=> Levels look healthy.")