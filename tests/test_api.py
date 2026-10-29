"""Backend API tests. They never run Demucs: only request validation, the library, sessions, auth and export."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as backend  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    stems, library = tmp_path / "stems", tmp_path / "library"
    stems.mkdir(); library.mkdir()
    monkeypatch.setattr(backend, "STEMS_DIR", stems)
    monkeypatch.setattr(backend, "LIBRARY_DIR", library)
    monkeypatch.setattr(backend, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(backend, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(backend, "PASSCODE", "")
    monkeypatch.setattr(backend, "MAX_UPLOAD_MB", 500)
    return TestClient(backend.app)


def make_song(client, song_id="abc1234567", title="My Song", stems=("vocals", "bass")):
    d = backend.STEMS_DIR / song_id
    d.mkdir()
    for s in stems:
        (d / f"{s}.mp3").write_bytes(b"x" * 100)
    (d / "meta.json").write_text(json.dumps({"title": title, "model": "htdemucs", "created": 1000}))
    (backend.LIBRARY_DIR / f"{song_id}.mp3").write_bytes(b"y" * 50)
    return song_id


def upload(client, **form):
    return client.post("/api/demucs/separate", files={"file": ("a.mp3", b"abc", "audio/mpeg")}, data=form)


# ---- health / auth -------------------------------------------------------------------------------------------

def test_health_is_public_and_reports_auth(client, monkeypatch):
    assert client.get("/api/health").json()["auth"] is False
    monkeypatch.setattr(backend, "PASSCODE", "secret")
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["auth"] is True


def test_passcode_blocks_api_until_supplied(client, monkeypatch):
    monkeypatch.setattr(backend, "PASSCODE", "secret")
    assert client.get("/api/library").status_code == 401
    assert client.get("/api/library", headers={"X-QT-Key": "wrong"}).status_code == 401
    assert client.get("/api/library", headers={"X-QT-Key": "secret"}).status_code == 200
    assert client.options("/api/library", headers={"Origin": "http://x", "Access-Control-Request-Method": "GET"}).status_code == 200


def test_upload_size_limit(client, monkeypatch):
    monkeypatch.setattr(backend, "MAX_UPLOAD_MB", 0)
    assert upload(client, model="htdemucs", device="cpu").status_code == 413


# ---- separation request validation ---------------------------------------------------------------------------

@pytest.mark.parametrize("form,code", [
    ({"model": "nope", "device": "cpu"}, 400),
    ({"model": "htdemucs", "device": "tpu"}, 400),
    ({"model": "htdemucs", "device": "cpu", "stem": "kazoo"}, 400),
    ({"model": "htdemucs", "device": "cpu", "stem": "piano"}, 400),  # piano needs the 6-stem model
])
def test_separate_rejects_bad_requests(client, form, code):
    assert upload(client, **form).status_code == code


# ---- library -------------------------------------------------------------------------------------------------

def test_library_lists_songs_with_sizes(client):
    make_song(client)
    data = client.get("/api/library").json()
    assert [s["title"] for s in data["songs"]] == ["My Song"]
    song = data["songs"][0]
    assert set(song["outputs"]) == {"vocals", "bass"} and song["hasOriginal"] is True
    assert song["bytes"] >= 250 and data["total_bytes"] == sum(s["bytes"] for s in data["songs"])


def test_untitled_song_gets_a_default_name_and_can_be_renamed(client):
    d = backend.STEMS_DIR / "ffff000011"; d.mkdir(); (d / "drums.mp3").write_bytes(b"x")
    assert client.get("/api/library").json()["songs"][0]["title"] == "Untitled song"
    assert client.patch("/api/library/ffff000011", json={"title": "  Dragon "}).json()["title"] == "Dragon"
    assert client.get("/api/library").json()["songs"][0]["title"] == "Dragon"
    assert client.patch("/api/library/ffff000011", json={"title": " "}).status_code == 400
    assert client.patch("/api/library/0000000000", json={"title": "x"}).status_code == 404


def test_delete_one_and_many_and_ignore_bad_ids(client):
    a, b = make_song(client, "aaaaaaaaaa"), make_song(client, "bbbbbbbbbb")
    assert client.delete(f"/api/library/{a}").json() == {"deleted": a}
    assert not (backend.STEMS_DIR / a).exists() and not list(backend.LIBRARY_DIR.glob(a + ".*"))
    assert client.delete("/api/library/not-an-id").status_code == 400
    r = client.post("/api/library/delete", json={"ids": [b, "../x", 5]})
    assert r.json()["deleted"] == [b]
    assert client.get("/api/library").json()["songs"] == []
    assert client.post("/api/library/delete", json={"ids": "all"}).status_code == 400


# ---- sessions ------------------------------------------------------------------------------------------------

def test_sessions_round_trip_with_tombstones(client):
    assert client.get("/api/sessions").json() == {"sessions": [], "deleted": []}
    payload = {"sessions": [{"id": "s1", "title": "x"}], "deleted": ["old"]}
    assert client.put("/api/sessions", json=payload).json() == {"saved": 1}
    assert client.get("/api/sessions").json() == payload
    assert client.put("/api/sessions", json={"sessions": "nope"}).status_code == 400


# ---- export --------------------------------------------------------------------------------------------------

def test_export_validation(client):
    song = make_song(client)
    assert client.post("/api/export", json={"song_id": "0000000000", "gains": {}}).status_code == 404
    assert client.post("/api/export", json={"song_id": song, "gains": {"vocals": 0}}).status_code == 400
    assert client.post("/api/export", json={"song_id": song, "gains": {"vocals": "loud"}}).status_code == 400


def test_tempo_chain_stays_inside_atempo_limits():
    for t in (0.25, 0.5, 0.9, 1.0, 1.7, 3.0):
        stages = [float(p.split("=")[1]) for p in backend._tempo_chain(t).split(",")]
        assert all(0.5 <= s <= 2.0 for s in stages)
        product = 1.0
        for s in stages:
            product *= s
        assert product == pytest.approx(t, rel=1e-3)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_export_renders_an_mp3_with_the_requested_speed(client, tmp_path):
    song = make_song(client, stems=())
    d = backend.STEMS_DIR / song
    for name in ("vocals", "bass"):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                        "-b:a", "128k", str(d / f"{name}.mp3")], check=True)
    r = client.post("/api/export", json={"song_id": song, "gains": {"vocals": 0.8, "bass": 0.5}, "speed": 0.5})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    out = tmp_path / "mix.mp3"; out.write_bytes(r.content)
    if shutil.which("ffprobe"):
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                                   capture_output=True, text=True).stdout)
        assert 3.5 < dur < 4.6  # 2 s at half speed is about 4 s


# ---- key and chords ------------------------------------------------------------------------------------------

def test_analysis_is_cached_and_can_be_refreshed(client, monkeypatch):
    song = make_song(client)
    calls = []

    def fake(song_dir):
        calls.append(song_dir)
        return {"version": backend.chords.VERSION, "key": {"tonic": 0, "mode": "major", "confidence": 1},
                "duration": 10, "chords": [{"t": 0, "d": 10, "r": 0, "q": "maj"}]}

    monkeypatch.setattr(backend.chords, "analyze_song", fake)
    first = client.get(f"/api/analysis/{song}").json()
    assert first["key"]["mode"] == "major" and (backend.STEMS_DIR / song / "chords.json").exists()
    client.get(f"/api/analysis/{song}")
    assert len(calls) == 1                                   # second call came from the cache
    client.get(f"/api/analysis/{song}?refresh=1")
    assert len(calls) == 2


def test_analysis_errors(client, monkeypatch):
    assert client.get("/api/analysis/0000000000").status_code == 404
    assert client.get("/api/analysis/not-an-id").status_code == 404
    song = make_song(client)

    def too_short(_):
        raise ValueError("The song is too short to analyze.")

    monkeypatch.setattr(backend.chords, "analyze_song", too_short)
    assert client.get(f"/api/analysis/{song}").status_code == 422


# ---- backup and restore --------------------------------------------------------------------------------------

def test_backup_then_restore_brings_back_sessions_titles_and_client_data(client):
    song = make_song(client, title="Dragon")
    payload = {"sessions": [{"id": "s1", "title": "x"}], "deleted": []}
    client.put("/api/sessions", json=payload)
    r = client.post("/api/backup", json={"client": {"qt.layout": {"order": ["a"]}, "qt.key": "SECRET", "other": 1}})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert client.get("/api/backup/status").json()["count"] == 1

    client.put("/api/sessions", json={"sessions": [], "deleted": []})
    (backend.STEMS_DIR / song / "meta.json").write_text(json.dumps({"title": "Changed"}))
    out = client.post("/api/restore", files={"file": ("b.zip", r.content, "application/zip")}).json()
    assert out["sessions"] is True and out["songs"] == 1
    assert out["client"] == {"qt.layout": {"order": ["a"]}}            # the passcode and unknown keys are never saved
    assert client.get("/api/sessions").json()["sessions"] == payload["sessions"]
    assert json.loads((backend.STEMS_DIR / song / "meta.json").read_text())["title"] == "Dragon"
    assert client.get("/api/backup/status").json()["count"] == 2        # restoring first saves an undo point


def test_restore_rejects_junk_and_ignores_unexpected_files(client):
    import io, zipfile
    assert client.post("/api/restore", files={"file": ("b.zip", b"not a zip", "application/zip")}).status_code == 400
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({"app": "qt-stems"}))
        z.writestr("../evil.txt", "x")
        z.writestr("songs/zzzzzzzzzz/meta.json", "{}")
        z.writestr("songs/abc1234567/meta.json", json.dumps({"title": "Ghost"}))  # no such song here
    out = client.post("/api/restore", files={"file": ("b.zip", buf.getvalue(), "application/zip")}).json()
    assert out == {"client": {}, "sessions": False, "songs": 0}
    assert not (backend.STEMS_DIR / "abc1234567").exists() and not (backend.BACKUP_DIR.parent / "evil.txt").exists()
    other = io.BytesIO()
    with zipfile.ZipFile(other, "w") as z:
        z.writestr("manifest.json", json.dumps({"app": "something-else"}))
    assert client.post("/api/restore", files={"file": ("b.zip", other.getvalue(), "application/zip")}).status_code == 400


def test_backups_are_pruned_to_the_newest_ones(client, tmp_path):
    import backup as b
    for _ in range(b.KEEP + 3):
        b.make_backup(backend.BACKUP_DIR, backend.STEMS_DIR, backend.SESSIONS_FILE)
    assert len(b.list_backups(backend.BACKUP_DIR)) == b.KEEP
