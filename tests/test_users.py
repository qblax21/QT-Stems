"""Separate profiles: a friend's passcode shows only their own songs, sessions and downloads."""
import json
import sys
from pathlib import Path

import pytest
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as backend  # noqa: E402
import users  # noqa: E402
from test_api import client, make_song, upload  # noqa: E402,F401

OWN, ALEX = {"X-QT-Key": "ownerkey1"}, {"X-QT-Key": "alexkey123"}


@pytest.fixture()
def two(client, monkeypatch, tmp_path):
    monkeypatch.setattr(backend, "PASSCODE", "ownerkey1")
    for route in backend.app.routes:  # the /stems mount was made at import time; aim it at this test's folder
        if getattr(route, "name", "") == "stems":
            monkeypatch.setattr(route, "app", StaticFiles(directory=backend.STEMS_DIR))
    users.save(backend.USERS_FILE, [{"id": "alex", "name": "Alex", "key": "alexkey123", "quota_mb": 5000, "max_upload_mb": 200}])
    mine = make_song(client, "aaaaaaaaaa", "Mine")
    theirs = make_song(client, "bbbbbbbbbb", "Alex's song")
    meta = backend.STEMS_DIR / theirs / "meta.json"
    meta.write_text(json.dumps({"title": "Alex's song", "created": 2000, "owner": "alex"}))
    return client, mine, theirs


def titles(client, headers):
    return [s["title"] for s in client.get("/api/library", headers=headers).json()["songs"]]


def test_each_person_sees_only_their_songs(two):
    client, mine, theirs = two
    assert titles(client, OWN) == ["Mine"]
    assert titles(client, ALEX) == ["Alex's song"]


def test_a_friend_cannot_reach_the_owners_song_by_any_route(two):
    client, mine, theirs = two
    assert client.get(f"/stems/{mine}/vocals.mp3", headers=ALEX).status_code == 404
    assert client.get(f"/stems/{theirs}/vocals.mp3", headers=ALEX).status_code == 200
    assert client.get(f"/stems/{theirs}/vocals.mp3", headers=OWN).status_code == 404  # strictly separate
    assert client.get(f"/api/analysis/{mine}", headers=ALEX).status_code == 404
    assert client.post("/api/export", json={"song_id": mine, "gains": {"vocals": 1}}, headers=ALEX).status_code == 404
    assert client.patch(f"/api/library/{mine}", json={"title": "x"}, headers=ALEX).status_code == 404
    assert client.post(f"/api/detail/{mine}", headers=ALEX).status_code == 404
    assert client.delete(f"/api/library/{mine}", headers=ALEX).status_code == 404
    assert client.post("/api/library/delete", json={"ids": [mine]}, headers=ALEX).json() == {"deleted": []}
    assert titles(client, OWN) == ["Mine"]  # nothing was touched


def test_old_songs_without_an_owner_belong_to_the_owner(two):
    client, mine, _ = two
    assert "owner" not in json.loads((backend.STEMS_DIR / mine / "meta.json").read_text())
    assert client.get(f"/stems/{mine}/vocals.mp3", headers=OWN).status_code == 200


def test_sessions_are_kept_per_person(two):
    client, *_ = two
    client.put("/api/sessions", json={"sessions": [{"id": "s1"}]}, headers=OWN)
    client.put("/api/sessions", json={"sessions": [{"id": "a1"}, {"id": "a2"}]}, headers=ALEX)
    assert [s["id"] for s in client.get("/api/sessions", headers=OWN).json()["sessions"]] == ["s1"]
    assert [s["id"] for s in client.get("/api/sessions", headers=ALEX).json()["sessions"]] == ["a1", "a2"]


def test_owner_only_pages_are_closed_to_friends(two):
    client, *_ = two
    for method, url in (("get", "/api/last-error"), ("get", "/api/backup/status"), ("post", "/api/backup")):
        assert getattr(client, method)(url, headers=ALEX, **({"json": {}} if method == "post" else {})).status_code == 403
    assert client.get("/api/backup/status", headers=OWN).status_code == 200


def test_who_am_i(two):
    client, *_ = two
    assert client.get("/api/auth-check", headers=ALEX).json()["user"] == {"id": "alex", "name": "Alex", "admin": False}
    assert client.get("/api/auth-check", headers=OWN).json()["user"]["admin"] is True


def test_a_removed_friend_is_locked_out_immediately(two):
    client, *_ = two
    assert client.get("/api/library", headers=ALEX).status_code == 200
    users.save(backend.USERS_FILE, [])
    assert client.get("/api/library", headers=ALEX).status_code == 401


def test_friends_exist_but_owner_has_no_passcode_still_requires_a_passcode(client, monkeypatch):
    users.save(backend.USERS_FILE, [{"id": "alex", "name": "Alex", "key": "alexkey123"}])
    assert client.get("/api/library").status_code == 401
    assert client.get("/api/library", headers=ALEX).status_code == 200
    assert client.get("/api/health").json()["auth"] is True


def test_a_friend_cannot_pose_as_the_owner(two):
    client, *_ = two
    users.save(backend.USERS_FILE, [{"id": "owner", "name": "Me", "key": "sneaky12345"}])  # ignored: the owner id is reserved
    assert client.get("/api/library", headers={"X-QT-Key": "sneaky12345"}).status_code == 401


def test_wrong_passcodes_lock_an_address_out_for_a_while(two):
    client, *_ = two
    for _ in range(backend.MAX_FAILS):
        assert client.get("/api/library", headers={"X-QT-Key": "nope-nope"}).status_code == 401
    assert client.get("/api/library", headers={"X-QT-Key": "nope-nope"}).status_code == 429
    assert client.get("/api/library", headers=OWN).status_code == 429  # even the right one, from the same address


def test_asking_without_a_passcode_does_not_count_as_guessing(two):
    client, *_ = two
    for _ in range(backend.MAX_FAILS + 5):
        assert client.get("/api/library").status_code == 401
    assert client.get("/api/library", headers=OWN).status_code == 200


def test_jobs_belong_to_whoever_started_them(two, monkeypatch):
    client, *_ = two
    monkeypatch.setattr(backend, "run_job", lambda *a, **k: None)
    job = client.post("/api/demucs/separate", files={"file": ("a.mp3", b"abc", "audio/mpeg")}, headers=ALEX).json()["job_id"]
    assert client.get(f"/api/jobs/{job}", headers=ALEX).status_code == 200
    assert client.get(f"/api/jobs/{job}", headers=OWN).status_code == 404


def test_friend_queue_storage_and_upload_limits(two, monkeypatch):
    client, *_ = two
    monkeypatch.setattr(backend, "run_job", lambda *a, **k: None)
    send = lambda: client.post("/api/demucs/separate", files={"file": ("a.mp3", b"abc", "audio/mpeg")}, headers=ALEX)  # noqa: E731
    assert send().status_code == 200 and send().status_code == 200
    assert send().status_code == 429  # two waiting is the most a friend may have
    backend.JOBS.clear()
    users.save(backend.USERS_FILE, [{"id": "alex", "name": "Alex", "key": "alexkey123", "quota_mb": 0.0001, "max_upload_mb": 200}])
    assert send().status_code == 403  # already over their small allowance
    users.save(backend.USERS_FILE, [{"id": "alex", "name": "Alex", "key": "alexkey123", "quota_mb": 5000, "max_upload_mb": 1}])
    big = client.post("/api/demucs/separate", files={"file": ("a.mp3", b"x" * (2 * 1024 * 1024), "audio/mpeg")}, headers=ALEX)
    assert big.status_code == 413 and "1 MB" in big.json()["detail"]


def test_a_friends_library_reports_their_allowance_as_free_space(two):
    client, *_ = two
    users.save(backend.USERS_FILE, [{"id": "alex", "name": "Alex", "key": "alexkey123", "quota_mb": 1, "max_upload_mb": 200}])
    lib = client.get("/api/library", headers=ALEX).json()
    assert lib["disk_free_bytes"] == 1_000_000 - lib["total_bytes"]


def test_a_finished_song_is_stamped_with_its_owner(client, tmp_path, monkeypatch):
    src = tmp_path / "in" / "song.mp3"
    src.parent.mkdir()
    src.write_bytes(b"abc")
    out = tmp_path / "work"
    (out / "htdemucs" / "song").mkdir(parents=True)
    (out / "htdemucs" / "song" / "vocals.mp3").write_bytes(b"v")
    monkeypatch.setattr(backend, "run_demucs", lambda *a, **k: None)
    monkeypatch.setattr(backend, "_detail_quietly", lambda *a, **k: None)
    backend.JOBS["cccccccccc"] = {"status": "queued", "_total": 0, "_done": 0}
    backend.run_job("cccccccccc", src, out, "htdemucs", "cpu", "", "Hi", "alex")
    assert users.song_owner(backend.STEMS_DIR / "cccccccccc") == "alex"


# ---- the users.py tool ---------------------------------------------------------------------------------------

def test_cli_add_list_reset_remove(tmp_path, monkeypatch, capsys):
    f, stems, lib = tmp_path / "users.json", tmp_path / "stems", tmp_path / "library"
    stems.mkdir(); lib.mkdir()
    monkeypatch.setenv("QT_PASSCODE", "ownerkey1")
    run = lambda *a: users.main(list(a), users_file=f, stems_dir=stems, library_dir=lib)  # noqa: E731
    assert run("add", "Alex B") == 0
    person = users.load(f)[0]
    assert person["id"] == "alex-b" and len(person["key"]) == users.KEY_LENGTH and person["key"] in capsys.readouterr().out
    assert run("add", "alex b") == 1  # duplicate
    song = stems / "dddddddddd"
    song.mkdir()
    (song / "meta.json").write_text(json.dumps({"owner": "alex-b"}))
    (song / "vocals.mp3").write_bytes(b"x" * 10)
    (lib / "dddddddddd.mp3").write_bytes(b"y" * 5)
    assert run("list") == 0 and "1 songs" in capsys.readouterr().out
    old = person["key"]
    assert run("reset", "alex-b") == 0 and users.load(f)[0]["key"] != old
    assert run("remove", "alex-b") == 0 and users.load(f) == [] and song.is_dir()
    assert run("add", "Alex B") == 0
    assert run("remove", "alex-b", "--delete-songs") == 0 and not song.exists() and not (lib / "dddddddddd.mp3").exists()
    assert run("remove", "nobody") == 1


def test_cli_refuses_to_add_friends_while_the_owner_has_no_passcode(tmp_path, monkeypatch):
    monkeypatch.delenv("QT_PASSCODE", raising=False)
    monkeypatch.setattr(users, "BASE_DIR", tmp_path)  # no passcode.txt here
    assert users.main(["add", "Alex"], users_file=tmp_path / "users.json", stems_dir=tmp_path, library_dir=tmp_path) == 1
    assert not (tmp_path / "users.json").exists()
