"""Make the stems add back up to the uploaded song.

Demucs stems never sum to exactly the original: the sum is a little quieter and differs by the separation error plus
MP3 coding. The difference (original minus the sum of all stems) is saved as one more stem, "detail". With every
stem and the detail at full level you hear the uploaded song itself, and muting one stem gives "the original minus
that instrument", which usually sounds cleaner than adding up the other stems.
"""
import subprocess
from pathlib import Path

import numpy as np

SR = 44100
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".ogg"}
MAX_MINUTES = 30
MAX_LAG = 6000  # samples; MP3 stems come back about 1100 samples late


class Mismatch(Exception):
    """The upload and the stems do not line up well enough to trust a subtraction."""


def decode(path: Path, ffmpeg: str, sr: int = SR) -> np.ndarray:
    r = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-vn", "-ac", "2", "-ar", str(sr), "-f", "f32le", "-"],
                       capture_output=True)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[-300:] or f"ffmpeg could not decode {path.name}")
    return np.frombuffer(r.stdout, dtype=np.float32).reshape(-1, 2)


def find_lag(orig: np.ndarray, total: np.ndarray, sr: int = SR) -> tuple[int, float]:
    """k such that orig[t + k] ~ total[t], and the normalised correlation there. Tries a few loud passages."""
    n = min(len(orig), len(total))
    seg = 20 * sr
    if n < seg + 2 * MAX_LAG:
        raise Mismatch("The song is too short to line up.")
    best = (0, 0.0)
    for frac in (0.15, 0.4, 0.65, 0.85):
        s = int(frac * (n - seg - 2 * MAX_LAG)) + MAX_LAG
        a = orig[s - MAX_LAG:s + seg + MAX_LAG].mean(axis=1)
        b = np.zeros_like(a)
        b[MAX_LAG:MAX_LAG + seg] = total[s:s + seg].mean(axis=1)
        denom = float(np.sqrt((a[MAX_LAG:MAX_LAG + seg] ** 2).sum() * (b ** 2).sum()))
        if denom < 1e-6:
            continue
        size = 1 << int(np.ceil(np.log2(len(a) * 2)))
        c = np.fft.irfft(np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size)), size)
        lags = np.concatenate([np.arange(0, MAX_LAG + 1), np.arange(-MAX_LAG, 0)])
        vals = np.concatenate([c[:MAX_LAG + 1], c[size - MAX_LAG:]])
        i = int(vals.argmax())
        corr = float(vals[i] / denom)
        if corr > best[1]:
            best = (int(lags[i]), corr)
        if corr > 0.85:
            break
    return best


def build_detail(song_dir: Path, original: Path, out_path: Path, ffmpeg: str) -> dict:
    """Write out_path (an MP3) holding original - sum(stems). Raises Mismatch if the two do not belong together."""
    stems = [p for p in sorted(song_dir.iterdir()) if p.suffix.lower() in AUDIO_EXTS and p.stem != "detail"]
    if not stems:
        raise Mismatch("No stems to compare with.")
    total = None
    for p in stems:
        x = decode(p, ffmpeg)
        if total is None:
            total = x.copy()
        else:
            if len(x) > len(total):
                total = np.pad(total, ((0, len(x) - len(total)), (0, 0)))
            total[:len(x)] += x
        del x
    if len(total) > MAX_MINUTES * 60 * SR:
        raise Mismatch(f"Songs longer than {MAX_MINUTES} minutes are not supported.")
    orig = decode(original, ffmpeg)
    lag, corr = find_lag(orig, total)
    if corr < 0.6:
        raise Mismatch(f"The stems do not match the uploaded file (match {corr:.2f}).")

    n = len(total)
    aligned = np.zeros_like(total)
    lo, hi = max(0, -lag), min(n, len(orig) - lag)  # t range where orig[t + lag] exists
    if hi > lo:
        aligned[lo:hi] = orig[lo + lag:hi + lag]
    residual = np.clip(aligned - total, -1.0, 1.0)
    e_orig = float((aligned ** 2).sum()) or 1e-9
    snr_before = 10 * np.log10(e_orig / max(float(((aligned - total) ** 2).sum()), 1e-9))
    del orig, aligned

    tmp = out_path.with_suffix(".part.mp3")
    enc = subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "-",
                          "-b:a", "192k", str(tmp)], input=residual.astype("<f4").tobytes(), capture_output=True)
    if enc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(enc.stderr.decode("utf-8", "replace")[-300:] or "ffmpeg could not encode the detail track")
    tmp.replace(out_path)
    return {"lag": lag, "match": round(corr, 3), "snr_before_db": round(float(snr_before), 1)}
