"""
voice_security/features.py - speaker features in pure numpy (no librosa / torch needed).

An utterance becomes a 40-number "voice vector" (the 2 pitch values are NaN when pitch cannot be measured):
    mean and std of MFCC 1..19 (vocal-tract shape / timbre)  +  mean and std of log-F0 (pitch)
Silent frames are dropped first. This is a lightweight classical front end; see voice_auth.py for what
it can and cannot protect against.
"""
from __future__ import annotations

import numpy as np

SR = 16000
N_MFCC = 20
_FRAME, _HOP, _NFFT, _N_MELS = 400, 160, 512, 40


def _hz_to_mel(f):
    return 2595.0 * np.log10(1.0 + np.asarray(f) / 700.0)


def _mel_to_hz(m):
    return 700.0 * (10.0 ** (np.asarray(m) / 2595.0) - 1.0)


def mel_filterbank(sr: int = SR, n_fft: int = _NFFT, n_mels: int = _N_MELS, fmin: float = 80.0, fmax: float = 7600.0):
    pts = _mel_to_hz(np.linspace(_hz_to_mel(fmin), _hz_to_mel(fmax), n_mels + 2))
    bins = np.floor((n_fft + 1) * pts / sr).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1))
    for i in range(1, n_mels + 1):
        l, c, r = bins[i - 1], bins[i], bins[i + 1]
        for k in range(l, c):
            fb[i - 1, k] = (k - l) / max(c - l, 1)
        for k in range(c, r):
            fb[i - 1, k] = (r - k) / max(r - c, 1)
    return fb


def _dct_matrix(n_mfcc: int = N_MFCC, n_mels: int = _N_MELS):
    n = np.arange(n_mels)
    k = np.arange(n_mfcc)[:, None]
    return np.sqrt(2.0 / n_mels) * np.cos(np.pi * k * (2 * n + 1) / (2 * n_mels))


_FB, _DCT, _WIN = mel_filterbank(), _dct_matrix(), np.hamming(_FRAME)


def _frames(x: np.ndarray, size: int, hop: int) -> np.ndarray:
    n = 1 + max(0, (len(x) - size) // hop)
    idx = np.arange(size)[None, :] + hop * np.arange(n)[:, None]
    return x[idx]


def mfcc_frames(audio: np.ndarray):
    """(frames x N_MFCC) cepstra and per-frame log energy."""
    x = np.asarray(audio, dtype=np.float64)
    x = np.append(x[:1], x[1:] - 0.97 * x[:-1])          # pre-emphasis
    fr = _frames(x, _FRAME, _HOP) * _WIN
    spec = np.abs(np.fft.rfft(fr, _NFFT, axis=1)) ** 2
    logmel = np.log(spec @ _FB.T + 1e-10)
    return logmel @ _DCT.T, np.log(spec.sum(axis=1) + 1e-10)


def f0_track(audio: np.ndarray, sr: int = SR, fmin: float = 70.0, fmax: float = 400.0) -> np.ndarray:
    """Pitch (Hz) of voiced 40 ms frames by autocorrelation."""
    fs, hop = 640, 320
    x = np.asarray(audio, dtype=np.float64)
    n = 1 + (len(x) - fs) // hop if len(x) >= fs else 0
    if n <= 0:
        return np.zeros(0)
    win = np.hanning(fs)
    frames = [x[i * hop:i * hop + fs] for i in range(n)]
    rms = np.array([np.sqrt(np.mean(f ** 2)) for f in frames])
    lo, hi = int(sr / fmax), int(sr / fmin)
    out = []
    for f, r in zip(frames, rms):
        if r < 0.25 * rms.max():
            continue
        spec = np.fft.rfft(f * win, 2 * fs)
        ac = np.fft.irfft(np.abs(spec) ** 2)[:hi + 1]
        ac = ac / (ac[0] + 1e-12)
        seg = ac[lo:hi + 1]
        k = int(np.argmax(seg))
        if seg[k] > 0.4:
            out.append(sr / (lo + k))
    return np.array(out)


def utterance_vector(audio: np.ndarray) -> np.ndarray:
    """40-dimensional voice vector for one utterance (16 kHz mono float)."""
    audio = np.asarray(audio, dtype=np.float64)
    if len(audio) < _FRAME * 3:
        raise ValueError("audio too short for voice features")
    c, energy = mfcc_frames(audio)
    keep = energy > energy.max() - 8.0                    # drop frames > ~35 dB below the loudest
    c = c[keep][:, 1:]                                    # skip c0 (loudness)
    f0 = f0_track(audio)
    if len(f0) >= 5:
        lf = np.log(f0)
        pitch = [lf.mean(), lf.std()]
    else:                                                 # whispered / too short: pitch unknown (NaN = skipped)
        pitch = [np.nan, np.nan]
    return np.concatenate([c.mean(axis=0), c.std(axis=0), pitch])
