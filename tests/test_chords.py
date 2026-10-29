"""Chord and key estimation on synthetic audio (no ffmpeg or Demucs needed)."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import chords  # noqa: E402

SR = chords.SR


def note(midi, seconds, harmonics=(1.0, 0.5, 0.3, 0.2), cents=0.0):
    f = 440.0 * 2 ** ((midi - 69 + cents / 100) / 12)
    t = np.arange(int(seconds * SR)) / SR
    return sum(a * np.sin(2 * np.pi * f * (i + 1) * t) for i, a in enumerate(harmonics)).astype(np.float32)


def chord(root_midi, ivs, seconds, cents=0.0):
    return sum(note(root_midi + i, seconds, cents=cents) for i in ivs) / len(ivs)


def progression(spec, seconds=2.0, cents=0.0):
    """spec: list of (root pitch class, 'maj'|'min'). Returns (harmony, bass)."""
    harm, bass = [], []
    for root, q in spec:
        ivs = [0, 4, 7] if q == "maj" else [0, 3, 7]
        harm.append(chord(60 + root, ivs, seconds, cents))
        bass.append(note(36 + root, seconds, harmonics=(1.0, 0.4), cents=cents))
    return np.concatenate(harm), np.concatenate(bass)


def names(result):
    return [(c["r"], c["q"]) for c in result["chords"] if c["q"] != "N"]


def collapse(seq):
    out = []
    for x in seq:
        if not out or out[-1] != x:
            out.append(x)
    return out


def test_finds_c_major_and_the_four_chords():
    spec = [(0, "maj"), (9, "min"), (5, "maj"), (7, "maj")] * 3
    harm, bass = progression(spec)
    result = chords.analyze_signal(harm, bass)
    assert result["key"]["tonic"] == 0 and result["key"]["mode"] == "major"
    assert collapse(names(result)) == collapse(spec)
    # chord changes land on the 2-second grid
    starts = [c["t"] for c in result["chords"]]
    assert all(min(abs(s - 2 * round(s / 2)) for s in [t]) < 0.4 for t in starts)


def test_finds_a_minor_without_a_bass_track():
    spec = [(9, "min"), (5, "maj"), (0, "maj"), (7, "maj")] * 3
    harm, _ = progression(spec)
    result = chords.analyze_signal(harm, None)
    assert result["key"]["mode"] in ("minor", "major") and result["key"]["tonic"] in (9, 0)
    assert collapse(names(result)) == collapse(spec)


def test_handles_a_song_tuned_a_bit_flat():
    spec = [(2, "maj"), (7, "maj"), (4, "min"), (9, "maj")] * 3
    harm, bass = progression(spec, cents=-35)
    result = chords.analyze_signal(harm, bass)
    assert result["key"]["tonic"] == 2 and result["key"]["mode"] == "major"
    assert collapse(names(result)) == collapse(spec)


def test_silence_is_no_chord_and_short_audio_is_rejected():
    harm = np.concatenate([chord(60, [0, 4, 7], 4), np.zeros(SR * 4, dtype=np.float32), chord(60, [0, 4, 7], 4)])
    result = chords.analyze_signal(harm)
    assert any(c["q"] == "N" for c in result["chords"])
    with pytest.raises(ValueError):
        chords.analyze_signal(np.zeros(SR, dtype=np.float32))


def test_picks_harmony_stems_and_leaves_out_drums_and_vocals():
    files = {n: Path(n) for n in ("vocals", "drums", "bass", "other", "guitar")}
    harm, bass = chords.pick_stems(files)
    assert sorted(p.name for p in harm) == ["guitar", "other"] and [p.name for p in bass] == ["bass"]
    two = {n: Path(n) for n in ("vocals", "no_vocals")}
    assert [p.name for p in chords.pick_stems(two)[0]] == ["no_vocals"]
    only = {"drums": Path("drums")}
    assert [p.name for p in chords.pick_stems(only)[0]] == ["drums"]
