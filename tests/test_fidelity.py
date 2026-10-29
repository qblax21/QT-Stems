"""The detail track: original minus the stems, so everything together equals the upload."""
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fidelity  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
SR = fidelity.SR


def tone_mix(seconds=30, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 0.3 * t)
    a = (0.30 * env * np.sin(2 * np.pi * 220 * t))[:, None] * np.ones((1, 2))
    b = (0.25 * np.sin(2 * np.pi * 330 * t + 1) * (np.sin(2 * np.pi * 1.7 * t) > 0))[:, None] * np.array([[1.0, 0.6]])
    missed = 0.08 * rng.standard_normal((len(t), 2))                        # what the "split" did not capture
    return a.astype(np.float32), b.astype(np.float32), missed.astype(np.float32)


def delayed(x, n):
    return np.concatenate([np.zeros((n, 2), np.float32), x])[:len(x)]


def test_detail_restores_the_original_despite_the_mp3_delay(tmp_path):
    a, b, missed = tone_mix()
    song, stems = tmp_path / "song", tmp_path / "orig.wav"
    song.mkdir()
    sf.write(stems, a + b + missed, SR)
    sf.write(song / "vocals.wav", delayed(a, 1105), SR)     # stems come back about 1105 samples late
    sf.write(song / "other.wav", delayed(b, 1105), SR)
    out = song / "detail.mp3"
    info = fidelity.build_detail(song, stems, out, shutil.which("ffmpeg"))
    assert info["lag"] == -1105 and info["match"] > 0.9 and out.exists()

    ff = shutil.which("ffmpeg")
    total = fidelity.decode(song / "vocals.wav", ff) + fidelity.decode(song / "other.wav", ff)
    detail = fidelity.decode(out, ff)
    n = min(len(total), len(detail))
    mixed = total[:n] + detail[:n]
    m = n - 1105 - 2000
    want = fidelity.decode(stems, ff)[:m]                 # stems and detail sit 1105 samples later than the upload
    got, plain = mixed[1105:1105 + m], total[1105:1105 + m]
    err = float(((want - got) ** 2).sum() / (want ** 2).sum())
    plain_err = float(((want - plain) ** 2).sum() / (want ** 2).sum())
    assert plain_err > 0.05                                # the stems alone miss a real part of the upload
    assert err < plain_err / 3                             # stems + detail get most of it back (the rest is MP3 noise)


def test_unrelated_audio_is_refused(tmp_path):
    a, b, _ = tone_mix(seed=1)
    song = tmp_path / "song"
    song.mkdir()
    sf.write(song / "vocals.wav", a, SR)
    rng = np.random.default_rng(5)
    sf.write(tmp_path / "other.wav", (0.3 * rng.standard_normal(a.shape)).astype(np.float32), SR)
    with pytest.raises(fidelity.Mismatch):
        fidelity.build_detail(song, tmp_path / "other.wav", song / "detail.mp3", shutil.which("ffmpeg"))
    assert not (song / "detail.mp3").exists()
