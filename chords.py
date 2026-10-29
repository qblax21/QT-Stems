"""Key and chord estimation from the separated stems. numpy only (ffmpeg decodes the audio).

The pipeline: mix the harmonic stems (everything except drums, vocals and bass) to mono, take a tuning-corrected
chroma (pitch-class energy) per frame, estimate the key with Krumhansl-Schmuckler profiles, then pick one chord per
frame by template matching and smooth the result with a Viterbi pass that prefers staying on a chord and prefers
chords that belong to the key. The bass stem, when there is one, adds weight to the chord's root.
It is an estimate: expect it to be right most of the time on simple pop/rock, and to simplify jazz harmony.
"""
import shutil
import subprocess
from pathlib import Path

import numpy as np

VERSION = 2
TEMPO_HOP = 256
TEMPO_NFFT = 1024
SR = 11025
HOP = 1024
NFFT_HARM = 4096
NFFT_BASS = 8192
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".ogg"}

MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
SHARP_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FLAT_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
FLAT_MAJOR_TONICS = {5, 10, 3, 8, 1, 6}  # F Bb Eb Ab Db Gb
FLAT_MINOR_TONICS = {2, 7, 0, 5, 10, 3}  # d g c f bb eb

# chord quality -> (intervals, template weights, penalty). Sevenths must be clearly present to win over a triad.
QUALITIES = {
    "maj": ([0, 4, 7], [1.0, 0.8, 0.9], 0.0),
    "min": ([0, 3, 7], [1.0, 0.8, 0.9], 0.0),
    "7": ([0, 4, 7, 10], [1.0, 0.8, 0.9, 0.75], 0.07),
    "m7": ([0, 3, 7, 10], [1.0, 0.8, 0.9, 0.75], 0.07),
}
MAJOR_SCALE = {0, 2, 4, 5, 7, 9, 11}
MINOR_SCALE = {0, 2, 3, 5, 7, 8, 10}


def _templates():
    """-> (list of (root, quality), matrix [n, 12] of unit-length templates, penalty vector)."""
    labels, rows, pens = [], [], []
    for root in range(12):
        for q, (ivs, ws, pen) in QUALITIES.items():
            v = np.zeros(12)
            for iv, w in zip(ivs, ws):
                v[(root + iv) % 12] = w
            labels.append((root, q)); rows.append(v / np.linalg.norm(v)); pens.append(pen)
    return labels, np.array(rows), np.array(pens)


LABELS, TEMPLATES, PENALTIES = _templates()


def decode_mono(path: Path, ffmpeg: str, sr: int = SR) -> np.ndarray:
    result = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                            capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[-300:] or "ffmpeg could not decode the stem")
    return np.frombuffer(result.stdout, dtype=np.float32).copy()


def _band_magnitudes(x: np.ndarray, nfft: int, lo: float, hi: float, hop: int = HOP, sr: int = SR):
    """Compressed magnitude spectrum per frame, restricted to lo..hi Hz. Frame i is centred on sample i*hop."""
    x = np.pad(x.astype(np.float32), (nfft // 2, nfft // 2))
    n = 1 + (len(x) - nfft) // hop
    freqs = np.arange(nfft // 2 + 1) * sr / nfft
    keep = np.where((freqs >= lo) & (freqs <= hi))[0]
    win = np.hanning(nfft).astype(np.float32)
    out = np.empty((n, len(keep)), dtype=np.float32)
    block = 256
    for s in range(0, n, block):
        e = min(n, s + block)
        idx = np.arange(s, e)[:, None] * hop + np.arange(nfft)[None, :]
        spec = np.abs(np.fft.rfft(x[idx] * win, axis=1))[:, keep]
        out[s:e] = np.sqrt(spec)  # compress so a few loud notes don't swamp the rest
    return out, freqs[keep]


def _chroma_weights(freqs: np.ndarray, tuning: float) -> np.ndarray:
    midi = 69 + 12 * np.log2(freqs / 440.0) - tuning
    near = np.round(midi)
    w = np.maximum(0.0, 1.0 - 2.0 * np.abs(midi - near))
    out = np.zeros((12, len(freqs)), dtype=np.float32)
    out[np.mod(near, 12).astype(int), np.arange(len(freqs))] = w
    return out


def _chroma(mags: np.ndarray, freqs: np.ndarray):
    """Pick the tuning offset (semitones) that lines the spectral peaks up best, then fold to 12 pitch classes."""
    mean = mags.mean(axis=0)
    best, best_t = -1.0, 0.0
    for t in (-0.4, -0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3, 0.4):
        score = float((_chroma_weights(freqs, t) @ mean).sum())
        if score > best:
            best, best_t = score, t
    return mags @ _chroma_weights(freqs, best_t).T, best_t


def estimate_key(chroma_mean: np.ndarray):
    """-> (tonic pitch class, 'major'|'minor', margin between the best and the runner-up in 0..1)."""
    scores = []
    for tonic in range(12):
        for mode, prof in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
            r = np.corrcoef(chroma_mean, np.roll(prof, tonic))[0, 1]
            scores.append((r if np.isfinite(r) else -1.0, tonic, mode))
    scores.sort(reverse=True)
    best, second = scores[0], scores[1]
    return best[1], best[2], float(max(0.0, min(1.0, (best[0] - second[0]) * 4)))


def onset_envelope(x: np.ndarray, sr: int = SR) -> np.ndarray:
    """Spectral-flux onset strength per TEMPO_HOP samples, with the slow average removed and scaled to 0..1."""
    mags, _ = _band_magnitudes(x, TEMPO_NFFT, 40.0, 5000.0, hop=TEMPO_HOP, sr=sr)
    flux = np.concatenate([[0.0], np.maximum(np.diff(mags, axis=0), 0).sum(axis=1)])
    w = max(3, int(round(0.5 * sr / TEMPO_HOP)) | 1)
    env = np.maximum(flux - np.convolve(flux, np.ones(w) / w, mode="same"), 0)
    peak = env.max()
    return env / peak if peak > 0 else env


def _comb(env: np.ndarray, period: float, phases: np.ndarray) -> np.ndarray:
    """Mean onset strength on a beat grid with the given period (frames) for each candidate phase (frames)."""
    n = len(env)
    k = np.arange(int(n / period) + 1)
    pos = np.round(phases[:, None] + k[None, :] * period).astype(int)
    ok = pos < n
    vals = np.where(ok, env[np.minimum(pos, n - 1)], 0.0)
    return vals.sum(axis=1) / np.maximum(ok.sum(axis=1), 1)


def estimate_tempo(x: np.ndarray, sr: int = SR) -> dict | None:
    """Constant-tempo estimate: {'bpm', 'offset' (seconds of the first beat), 'confidence' 0..1}, or None."""
    env = onset_envelope(x, sr)
    n = len(env)
    fps = sr / TEMPO_HOP
    if n < fps * 6 or env.max() <= 0:
        return None
    spec = np.fft.rfft(env - env.mean(), 2 * n)
    ac = np.fft.irfft(np.abs(spec) ** 2)[:n]
    ac = ac / np.maximum(n - np.arange(n), 1)
    bpms = np.arange(60.0, 200.0, 0.25)
    lags = fps * 60.0 / bpms
    prior = np.exp(-0.5 * (np.log2(bpms / 110.0) / 0.9) ** 2)
    score = np.interp(lags, np.arange(n), ac) * prior
    rough = float(bpms[int(score.argmax())])
    # refine: search +-3% for the beat grid that lines up best with the onsets
    smooth = np.convolve(env, np.array([0.25, 0.5, 0.25]), mode="same")
    best = (-1.0, rough, 0.0)
    for bpm in np.arange(rough * 0.97, rough * 1.03, 0.05):
        period = fps * 60.0 / bpm
        phases = np.arange(0.0, period, 0.5)
        c = _comb(smooth, period, phases)
        i = int(c.argmax())
        if c[i] > best[0]:
            best = (float(c[i]), float(bpm), float(phases[i]))
    value, bpm, phase = best
    base = float(smooth.mean()) or 1e-9
    return {"bpm": round(bpm, 1), "offset": round(phase / fps, 3),
            "confidence": round(float(max(0.0, min(1.0, (value / base - 1.0) / 3.0))), 2)}


def _diatonic(tonic: int, mode: str) -> np.ndarray:
    scale = {(tonic + s) % 12 for s in (MAJOR_SCALE if mode == "major" else MINOR_SCALE)}
    out = np.zeros(len(LABELS))
    for i, (root, q) in enumerate(LABELS):
        notes = {(root + iv) % 12 for iv in QUALITIES[q][0][:3]}
        out[i] = 1.0 if notes <= scale else 0.0
    return out


def _viterbi(emit: np.ndarray, stay: float = 0.92) -> np.ndarray:
    """emit: [T, S] log-scores. Staying costs log(stay); any switch costs log((1-stay)/(S-1))."""
    T, S = emit.shape
    ls, lw = np.log(stay), np.log((1 - stay) / (S - 1))
    delta = emit[0].copy()
    back = np.zeros((T, S), dtype=np.int32)
    for t in range(1, T):
        top = int(delta.argmax())
        switch = delta[top] + lw
        stay_score = delta + ls
        take_stay = stay_score >= switch
        back[t] = np.where(take_stay, np.arange(S), top)
        delta = np.where(take_stay, stay_score, switch) + emit[t]
    path = np.empty(T, dtype=np.int32)
    path[-1] = int(delta.argmax())
    for t in range(T - 1, 0, -1):
        path[t - 1] = back[t, path[t]]
    return path


def analyze_signal(harm: np.ndarray, bass: np.ndarray | None = None, sr: int = SR, drums: np.ndarray | None = None) -> dict:
    """harm / bass / drums: mono float signals at `sr` (SR). Returns the key, chord segments and tempo."""
    if len(harm) < sr * 2:
        raise ValueError("The song is too short to analyze.")
    hop_s = HOP / sr
    hm, hf = _band_magnitudes(harm, NFFT_HARM, 82.0, 2200.0, sr=sr)
    chroma, tuning = _chroma(hm, hf)
    bass_chroma = None
    if bass is not None and len(bass) >= sr * 2:
        bm, bf = _band_magnitudes(bass, NFFT_BASS, 38.0, 330.0, sr=sr)
        bass_chroma, _ = _chroma(bm, bf)
        n = min(len(chroma), len(bass_chroma))
        chroma, bass_chroma = chroma[:n], bass_chroma[:n]

    energy = chroma.sum(axis=1)
    loud = energy > max(1e-9, 0.12 * float(np.median(energy)))
    unit = chroma / np.maximum(np.linalg.norm(chroma, axis=1, keepdims=True), 1e-9)
    # smooth over ~4 frames so a single noisy frame does not decide a chord
    k = np.ones(4) / 4
    smooth = np.stack([np.convolve(unit[:, c], k, mode="same") for c in range(12)], axis=1)
    smooth /= np.maximum(np.linalg.norm(smooth, axis=1, keepdims=True), 1e-9)

    use = loud if loud.any() else np.ones(len(unit), dtype=bool)
    glob = unit[use].mean(axis=0)
    glob = glob / max(glob.sum(), 1e-9)
    if bass_chroma is not None:
        bunit = bass_chroma / np.maximum(np.linalg.norm(bass_chroma, axis=1, keepdims=True), 1e-9)
        bmean = bunit[use].mean(axis=0)
        glob = glob + 0.5 * bmean / max(bmean.sum(), 1e-9)
    tonic, mode, margin = estimate_key(glob)

    score = smooth @ TEMPLATES.T - PENALTIES                      # [T, 48]
    if bass_chroma is not None:
        bsm = np.stack([np.convolve(bunit[:, c], k, mode="same") for c in range(12)], axis=1)
        roots = np.array([r for r, _ in LABELS])
        score = score + 0.25 * bsm[:, roots]
    score = score + 0.06 * _diatonic(tonic, mode)[None, :]
    emit = np.concatenate([score, np.where(loud, 0.45, 2.0)[:, None]], axis=1)
    emit[~loud, :-1] = 0.0
    path = _viterbi(15.0 * emit)

    segs = []
    start = 0
    for i in range(1, len(path) + 1):
        if i == len(path) or path[i] != path[start]:
            segs.append([start * hop_s, (i - start) * hop_s, int(path[start])])
            start = i
    # fold very short blips into the previous chord
    merged = []
    for seg in segs:
        if merged and seg[1] < 0.3:
            merged[-1][1] += seg[1]
        elif merged and merged[-1][2] == seg[2]:
            merged[-1][1] += seg[1]
        else:
            merged.append(seg)
    # B / B7 flicker on the same root reads as noise: fuse neighbours that differ only by the seventh
    related = {"maj": "7", "7": "maj", "min": "m7", "m7": "min"}
    fused = []  # each: [start, duration, state, {state: seconds}]
    for t, d, s in merged:
        prev = fused[-1] if fused else None
        if prev and s < len(LABELS) and prev[2] < len(LABELS):
            (r1, q1), (r2, q2) = LABELS[prev[2]], LABELS[s]
            if r1 == r2 and (q1 == q2 or related[q1] == q2):
                prev[3][s] = prev[3].get(s, 0.0) + d
                prev[1] += d
                prev[2] = max(prev[3], key=prev[3].get)
                continue
        fused.append([t, d, s, {s: d}])
    chords = []
    for t, d, s, _ in fused:
        if s == len(LABELS):
            root, q = -1, "N"
        else:
            root, q = LABELS[s]
        chords.append({"t": round(t, 2), "d": round(d, 2), "r": root, "q": q})
    return {
        "version": VERSION,
        "key": {"tonic": tonic, "mode": mode, "confidence": round(margin, 2)},
        "tuning": round(tuning, 2),
        "duration": round(len(harm) / sr, 2),
        "chords": chords,
        "tempo": estimate_tempo(drums if drums is not None and len(drums) >= sr * 6 else harm, sr),
    }


def pick_stems(files: dict) -> tuple[list, list]:
    """Which stem files carry the harmony and which the bass. Drums and vocals are left out."""
    harm = [p for n, p in files.items() if n not in ("drums", "vocals", "bass")]
    bass = [p for n, p in files.items() if n == "bass"]
    if not harm:
        harm = [p for n, p in files.items() if n != "drums"] or list(files.values())
    return harm, bass


def analyze_song(song_dir: Path) -> dict:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed on the PC.")
    files = {p.stem: p for p in sorted(song_dir.iterdir()) if p.suffix.lower() in AUDIO_EXTS}
    if not files:
        raise RuntimeError("This song has no stems to analyze.")
    harm_files, bass_files = pick_stems(files)
    drum_files = [p for n, p in files.items() if n == "drums"]

    def mix(paths):
        sigs = [decode_mono(p, ffmpeg) for p in paths]
        n = max(len(s) for s in sigs)
        total = np.zeros(n, dtype=np.float32)
        for s in sigs:
            total[:len(s)] += s
        return total

    return analyze_signal(mix(harm_files), mix(bass_files) if bass_files else None, drums=mix(drum_files) if drum_files else None)
