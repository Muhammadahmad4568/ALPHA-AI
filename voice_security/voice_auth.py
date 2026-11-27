"""
voice_security/voice_auth.py - voice biometric lock for Alpha.

    python -m voice_security.voice_auth enroll      record 3-5 phrases -> data/voice_profile.npz
    python -m voice_security.voice_auth test        speak and see live match scores (try other people too)
    python -m voice_security.voice_auth status      profile summary
    python -m voice_security.voice_auth reset       delete the profile

At runtime every utterance is checked by VoiceGate.check() BEFORE it is transcribed or acted on.
An unrecognised voice is logged and ignored; nothing is executed and the audio is dropped.

HONEST LIMITS - read this
  * Default backend ("mfcc"): a classical, dependency-free speaker model (timbre + pitch statistics),
    calibrated on YOUR enrollment samples. It reliably separates voices that differ clearly (other
    people in the room, TV), but it is a convenience lock, NOT strong security: similar voices can pass,
    short phrases ("yes") score unreliably, and a recording of your voice WILL pass (no anti-replay).
  * Optional backend ("speechbrain", ALPHA_VOICE_BACKEND=speechbrain): ECAPA-TDNN neural embeddings with
    cosine similarity. Much more accurate, but needs `pip install torch speechbrain` (~1 GB RAM when loaded).
    That backend could not be tested in the build environment.
  * MFCC backend decision: the voice must pass BOTH a timbre check (standardised distance of MFCC statistics
    from your enrollment centroid; cosine similarity of raw MFCC means barely separates speakers, so it is not
    used) AND a pitch check (average pitch within a tolerance calibrated from your own samples, +-13..28%).
    The shown score is 1/(1+worst ratio) and passes at >= 0.5. The neural backend uses true cosine similarity.
  * The profile is a .npz file (no pickle, so loading it can never execute code). It contains numbers
    derived from your voice, not recordings. Keep it private.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from config.settings import DATA_DIR, VOICE_PROFILE, env_float
from voice_security.features import SR, utterance_vector

log = logging.getLogger("alpha.voice_auth")

PHRASES = [
    "Alpha, open the task manager.",
    "Alpha, what is my screen time today?",
    "Alpha, send a message to my friend.",
    "Alpha, search the web for the latest news.",
    "Alpha, lock my computer and set the volume to forty.",
]


# =============================================================================
# Backends
# =============================================================================
class MfccBackend:
    name = "mfcc"

    def embed(self, audio: np.ndarray) -> np.ndarray:
        return utterance_vector(audio)


class SpeechBrainBackend:
    """Optional ECAPA-TDNN embeddings (pip install torch speechbrain). Not tested in the build environment."""
    name = "speechbrain"

    def __init__(self):
        import torch  # noqa: F401
        try:
            from speechbrain.inference.speaker import EncoderClassifier
        except ImportError:  # older speechbrain
            from speechbrain.pretrained import EncoderClassifier
        self._torch = torch
        self.model = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb", savedir=str(DATA_DIR / "models" / "ecapa"),
            run_opts={"device": "cpu"})

    def embed(self, audio: np.ndarray) -> np.ndarray:
        wav = self._torch.from_numpy(np.asarray(audio, dtype=np.float32)).unsqueeze(0)
        with self._torch.no_grad():
            emb = self.model.encode_batch(wav).squeeze().cpu().numpy()
        return emb / (np.linalg.norm(emb) + 1e-9)


def make_backend(name: str):
    if name == "speechbrain":
        return SpeechBrainBackend()
    return MfccBackend()


# =============================================================================
# Profile
# =============================================================================
N_TIMBRE = 38          # vector[:38] = MFCC mean/std (timbre); vector[38] = mean log-F0; vector[39] = std log-F0


@dataclass
class VoiceProfile:
    backend: str
    vectors: np.ndarray            # enrollment embeddings (n x d)
    centroid: np.ndarray
    scale: np.ndarray              # mfcc: per-dimension scale of the timbre dims
    threshold: float               # mfcc: max timbre distance; neural: min cosine similarity
    created: float
    meta: Dict                     # mfcc: pitch_tol (log-F0 units), accept_at; both: strictness

    # ---- scoring -----------------------------------------------------------
    @staticmethod
    def _timbre_dist(v: np.ndarray, centroid: np.ndarray, scale: np.ndarray) -> float:
        return float(np.sqrt(np.mean(((v[:N_TIMBRE] - centroid[:N_TIMBRE]) / scale[:N_TIMBRE]) ** 2)))

    def _worst(self, vec: np.ndarray) -> float:
        """Largest 'how far past the allowed limit' ratio of the two checks (<= 1 passes)."""
        worst = self._timbre_dist(vec, self.centroid, self.scale) / self.threshold
        tol = self.meta.get("pitch_tol")
        if tol and np.isfinite(vec[N_TIMBRE]) and np.isfinite(self.centroid[N_TIMBRE]):
            worst = max(worst, abs(vec[N_TIMBRE] - self.centroid[N_TIMBRE]) / tol)
        return worst

    def score(self, vec: np.ndarray, strictness: float = 1.0) -> Tuple[float, float, bool]:
        """(similarity 0..1, similarity threshold, accepted). strictness > 1 is more lenient."""
        if self.backend == "speechbrain":
            cos = float(np.dot(vec, self.centroid) / (np.linalg.norm(self.centroid) + 1e-9))
            thr = self.threshold / max(strictness, 0.2)
            return cos, thr, cos >= thr
        w = self._worst(vec) / (self.meta.get("accept_at", 1.0) * max(strictness, 0.2))
        return 1.0 / (1.0 + w), 0.5, w <= 1.0


def build_profile(vectors: List[np.ndarray], backend: str = "mfcc", impostors: Optional[List[np.ndarray]] = None,
                  strictness: float = 1.0) -> Tuple[VoiceProfile, Dict]:
    """Calibrate the acceptance limits from the enrollment samples (and optional impostor samples)."""
    V = np.vstack(vectors)
    n = len(V)
    if n < 3:
        raise ValueError("need at least 3 samples")
    report: Dict = {"samples": n, "backend": backend}
    if backend == "speechbrain":
        mu = V.mean(axis=0)
        mu_n = mu / (np.linalg.norm(mu) + 1e-9)

        def loo_cos(i: int) -> float:
            c = np.delete(V, i, 0).mean(0)
            return float(np.dot(V[i], c / (np.linalg.norm(c) + 1e-9)))

        genuine = [loo_cos(i) for i in range(n)]
        if impostors:
            imp = [float(np.dot(x, mu_n)) for x in impostors]
            thr = (min(genuine) + max(imp)) / 2 if min(genuine) > max(imp) else min(genuine) - 0.05
            report["impostor_scores"] = [round(x, 3) for x in imp]
        else:
            thr = float(np.clip(min(genuine) - 0.08, 0.25, 0.6))
        thr = float(np.clip(thr, 0.15, 0.8))
        report.update(genuine_scores=[round(g, 3) for g in genuine], threshold=round(thr, 3), unit="cosine similarity")
        return VoiceProfile("speechbrain", V, mu_n, np.ones_like(mu), thr, time.time(), {"strictness": strictness}), report

    # ---- classical backend: timbre distance + pitch tolerance ---------------
    with np.errstate(all="ignore"):
        mu = np.array([np.nanmean(V[:, j]) if np.isfinite(V[:, j]).sum() >= 3 else np.nan for j in range(V.shape[1])])
    sd = V[:, :N_TIMBRE].std(axis=0, ddof=1)
    scale = np.empty(N_TIMBRE)
    for g in (slice(0, 19), slice(19, 38)):             # floors per feature group, never across groups
        scale[g] = np.maximum(sd[g], 0.35 * sd[g].mean() + 1e-6)
    full_scale = np.concatenate([scale, np.ones(V.shape[1] - N_TIMBRE)])

    def timbre(v, c):
        return VoiceProfile._timbre_dist(v, c, full_scale)

    loo_t, loo_p = [], []
    for i in range(n):
        others = np.delete(V, i, 0)
        c = np.nanmean(others[:, :N_TIMBRE], axis=0)
        loo_t.append(timbre(V[i], np.concatenate([c, np.zeros(V.shape[1] - N_TIMBRE)])))
        pv = others[:, N_TIMBRE][np.isfinite(others[:, N_TIMBRE])]
        loo_p.append(abs(V[i, N_TIMBRE] - pv.mean()) if np.isfinite(V[i, N_TIMBRE]) and len(pv) else 0.0)
    thr_t = max(max(loo_t) * 1.3, 1.0)
    pitch_ok = bool(np.isfinite(mu[N_TIMBRE]))
    tol = float(np.clip(max(1.5 * max(loo_p), 0.12), 0.12, 0.25)) if pitch_ok else None   # +-13% .. +-28% of your usual pitch
    w_gen = [max(t / thr_t, (p / tol) if tol else 0.0) for t, p in zip(loo_t, loo_p)]
    accept_at = 1.0
    if impostors:
        w_imp = []
        for x in impostors:
            w = timbre(x, mu) / thr_t
            if tol and np.isfinite(x[N_TIMBRE]) and pitch_ok:
                w = max(w, abs(x[N_TIMBRE] - mu[N_TIMBRE]) / tol)
            w_imp.append(w)
        report["impostor_ratios"] = [round(w, 2) for w in w_imp]
        report["separable"] = bool(min(w_imp) > max(w_gen))
        if report["separable"]:
            accept_at = max(max(w_gen) * 1.05, (max(w_gen) + min(w_imp)) / 2)
            accept_at = min(accept_at, 1.0)               # calibration may only tighten the limits
    report.update(genuine_ratios=[round(w, 2) for w in w_gen], timbre_limit=round(thr_t, 2),
                  pitch_tolerance_percent=round((np.exp(tol) - 1) * 100) if tol else None,
                  tightened_to=round(accept_at, 2), unit="ratio to the allowed limit (<=1 accepts)")
    meta = {"strictness": strictness, "pitch_tol": tol, "accept_at": accept_at}
    return VoiceProfile("mfcc", V, mu, full_scale, float(thr_t), time.time(), meta), report


def save_profile(profile: VoiceProfile, path: Path = VOICE_PROFILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = json.dumps({"backend": profile.backend, "threshold": profile.threshold, "created": profile.created,
                       "n": len(profile.vectors), "version": 1, **profile.meta})
    tmp = path.with_suffix(".tmp.npz")
    np.savez_compressed(tmp, vectors=profile.vectors, centroid=profile.centroid, scale=profile.scale, meta=meta)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_profile(path: Path = VOICE_PROFILE) -> Optional[VoiceProfile]:
    if not Path(path).exists():
        return None
    with np.load(path, allow_pickle=False) as d:      # allow_pickle=False: a tampered file cannot run code
        meta = json.loads(str(d["meta"]))
        return VoiceProfile(meta["backend"], d["vectors"], d["centroid"], d["scale"], float(meta["threshold"]),
                            float(meta["created"]), {k: v for k, v in meta.items() if k not in ("backend", "threshold", "created")})


# =============================================================================
# Runtime gate
# =============================================================================
@dataclass
class Verdict:
    ok: bool
    score: float
    threshold: float
    reason: str


class VoiceGate:
    """
    ALPHA_VOICE_LOCK = strict (default): only the enrolled voice may command Alpha; with no profile, voice
                       commands are refused until you enroll (typing still works).
                       warn: verify and log, but never block.   off: no voice check.
    ALPHA_VOICE_STRICTNESS: >1 more lenient, <1 stricter (scales both the timbre and the pitch limit).
    ALPHA_VOICE_MIN_SECONDS: utterances shorter than this are not verifiable and are refused (default 0.5).
    """

    def __init__(self, path: Path = VOICE_PROFILE):
        self.mode = os.environ.get("ALPHA_VOICE_LOCK", "strict").strip().lower()
        self.min_seconds = env_float("ALPHA_VOICE_MIN_SECONDS", 0.5)
        self.strictness = env_float("ALPHA_VOICE_STRICTNESS", 1.0)
        self.profile: Optional[VoiceProfile] = None
        self.backend = None
        self.error = ""
        self.accepted = self.rejected = 0
        self.last: Optional[Dict] = None
        if self.mode != "off":
            try:
                self.profile = load_profile(path)
                if self.profile is not None:
                    self.backend = make_backend(self.profile.backend)
            except Exception as exc:                  # corrupt file / missing torch: fail CLOSED in strict mode
                self.error = str(exc)
                self.profile = None
                log.error("Voice profile could not be loaded: %s", exc)

    @property
    def enrolled(self) -> bool:
        return self.profile is not None

    def describe(self) -> str:
        if self.mode == "off":
            return "Voice lock: off"
        if self.profile is None:
            return "Voice lock: ON, but no voice profile - run enrollment" + (f" ({self.error})" if self.error else "")
        return f"Voice lock: {self.mode} ({self.profile.backend}, {len(self.profile.vectors)} samples)"

    def check(self, audio: np.ndarray) -> Verdict:
        seconds = len(audio) / SR
        if self.mode == "off":
            return Verdict(True, 1.0, 0.0, "voice lock off")
        if self.profile is None:
            v = Verdict(False, 0.0, 0.0, "no voice profile enrolled")
        elif seconds < self.min_seconds:
            v = Verdict(False, 0.0, 0.0, f"too short to verify ({seconds:.1f}s)")
        else:
            try:
                score, thr, ok = self.profile.score(self.backend.embed(audio), self.strictness)
                v = Verdict(ok, score, thr, "match" if ok else "voice does not match the enrolled owner")
            except Exception as exc:
                v = Verdict(False, 0.0, 0.0, f"verification error: {exc}")
        if self.mode == "warn":
            v = Verdict(True, v.score, v.threshold, v.reason + " (warn mode: allowed)")
        if v.ok:
            self.accepted += 1
        else:
            self.rejected += 1
        self.last = {"ok": v.ok, "score": round(v.score, 3), "threshold": round(v.threshold, 3), "reason": v.reason,
                     "seconds": round(seconds, 2), "ts": time.time()}
        return v

    def status(self) -> Dict:
        return {"mode": self.mode, "enrolled": self.enrolled, "backend": self.profile.backend if self.profile else None,
                "accepted": self.accepted, "rejected": self.rejected, "last": self.last, "summary": self.describe()}


# =============================================================================
# Enrollment / test CLI
# =============================================================================
def _capture(stt, count: int, prompts: List[str], stop: threading.Event) -> "queue.Queue[np.ndarray]":
    q: "queue.Queue" = queue.Queue()

    def worker() -> None:
        try:
            for audio in stt.utterances(gate=lambda: True, stop=stop):
                q.put(audio)
        except Exception as exc:
            q.put(exc)

    threading.Thread(target=worker, daemon=True, name="EnrollRecorder").start()
    return q


def _collect(stt, prompts: List[str], label: str, min_seconds: float = 1.2) -> List[np.ndarray]:
    stop = threading.Event()
    q = _capture(stt, len(prompts), prompts, stop)
    vectors: List[np.ndarray] = []
    print("\nCalibrating the microphone - please stay quiet for 2 seconds...")
    time.sleep(2.2)
    i = 0
    backend = make_backend(os.environ.get("ALPHA_VOICE_BACKEND", "mfcc").strip().lower())
    try:
        while i < len(prompts):
            while not q.empty():                      # drop audio recorded BEFORE this prompt (wrong speaker / old phrase)
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
            print(f"\n[{label} {i + 1}/{len(prompts)}] Say clearly, in your normal voice:\n    \"{prompts[i]}\"")
            try:
                item = q.get(timeout=30)
            except queue.Empty:
                print("  ...I didn't hear anything. Check your microphone, then speak after the prompt.")
                continue
            if isinstance(item, Exception):
                raise RuntimeError(f"microphone error: {item}")
            secs = len(item) / SR
            if secs < min_seconds:
                print(f"  Too short ({secs:.1f}s). Please say the whole phrase.")
                continue
            try:
                vectors.append(backend.embed(item))
            except Exception as exc:
                print(f"  Could not analyse that sample ({exc}). Try again.")
                continue
            print(f"  OK, captured {secs:.1f} s.")
            i += 1
    finally:
        stop.set()
    return vectors


def cmd_enroll(args) -> int:
    from voice.engine import STTEngine

    n = max(3, min(5, args.samples))
    backend_name = os.environ.get("ALPHA_VOICE_BACKEND", "mfcc").strip().lower()
    print(f"Voice enrollment ({backend_name} backend): you will record {n} short phrases.\n"
          "Sit where you normally use the PC, speak naturally, and keep the room quiet.")
    stt = STTEngine()
    stt.settings.auto_gain = True
    vectors = _collect(stt, PHRASES[:n], "Phrase")
    impostors: List[np.ndarray] = []
    if args.impostor:
        input("\nOptional calibration: ask ANOTHER person to sit at the mic. Press Enter when they are ready...")
        impostors = _collect(stt, PHRASES[:2], "Other person")
    profile, report = build_profile(vectors, backend_name, impostors or None,
                                    strictness=env_float("ALPHA_VOICE_STRICTNESS", 1.0))
    save_profile(profile)
    print("\nVoice profile saved to", VOICE_PROFILE)
    print(json.dumps(report, indent=2))
    if impostors and not report.get("separable", True):
        print("\n[!] The other person's voice overlapped yours. The lock will be lenient; consider the "
              "speechbrain backend (see README).")
    print("\nNext: python -m voice_security.voice_auth test   (speak, then let someone else speak, to see the scores)")
    return 0


def cmd_test(args) -> int:
    from voice.engine import STTEngine

    gate = VoiceGate()
    print(gate.describe())
    if not gate.enrolled:
        print("Enroll first: python -m voice_security.voice_auth enroll")
        return 1
    stt = STTEngine()
    stop = threading.Event()
    threading.Timer(args.seconds, stop.set).start()
    print(f"Speak for {args.seconds:.0f} s (then let someone else speak). Each phrase is scored:\n")
    for audio in stt.utterances(gate=lambda: True, stop=stop):
        v = gate.check(audio)
        print(f"  {'ACCEPTED' if v.ok else 'REJECTED':9} score {v.score:.2f} (needs >= {v.threshold:.2f})  {len(audio) / SR:.1f}s  - {v.reason}")
    print("\nTip: if YOU are rejected, set ALPHA_VOICE_STRICTNESS=1.3 in .env (more lenient) or re-enroll with more phrases.")
    return 0


def cmd_status(args) -> int:
    g = VoiceGate()
    print(g.describe())
    p = g.profile
    if p:
        print("created:", time.strftime("%Y-%m-%d %H:%M", time.localtime(p.created)), "| samples:", len(p.vectors),
              "| file:", VOICE_PROFILE, "| limits:", {k: (round(v, 3) if isinstance(v, float) else v) for k, v in p.meta.items()})
    return 0


def cmd_reset(args) -> int:
    if VOICE_PROFILE.exists():
        VOICE_PROFILE.unlink()
        print("Voice profile deleted.")
    else:
        print("No voice profile found.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="voice_auth", description="Alpha AI voice lock")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("enroll", help="record phrases and create the voice profile")
    e.add_argument("--samples", type=int, default=5, help="number of phrases, 3-5 (default 5)")
    e.add_argument("--impostor", action="store_true", help="also record another person to calibrate the threshold")
    e.set_defaults(fn=cmd_enroll)
    t = sub.add_parser("test", help="live scoring of whoever speaks")
    t.add_argument("--seconds", type=float, default=30.0)
    t.set_defaults(fn=cmd_test)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("reset").set_defaults(fn=cmd_reset)
    args = p.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
