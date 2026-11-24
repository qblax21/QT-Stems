"""The promises made to friends: 5 GB of songs, 2 songs at a time, 200 MB per upload, and a private library each."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as backend  # noqa: E402
import users  # noqa: E402
from test_api import client, make_song  # noqa: E402,F401
from test_users import ALEX, OWN, two  # noqa: E402,F401

BO = {"X-QT-Key": "bokey99999"}
MB = 1024 * 1024


def send(client, headers, size=3):
    return client.post("/api/demucs/separate", files={"file": ("a.mp3", b"x" * size, "audio/mpeg")}, headers=headers)


@pytest.fixture()
def fresh_friends(two, monkeypatch):
    """Alex and Bo created the way `users.py add` creates them (default limits), plus a do-nothing job runner."""
    client, *_ = two
    monkeypatch.setattr(backend, "run_job", lambda *a, **k: None)
    people = [{"id": "alex", "name": "Alex", "key": "alexkey123"}, {"id": "bo", "name": "Bo", "key": "bokey99999"}]
    users.save(backend.USERS_FILE, people)
    return client


def test_the_promised_defaults():
    assert users.DEFAULT_QUOTA_MB == 5000       # 5 GB of songs per friend
    assert users.DEFAULT_UPLOAD_MB == 200       # 200 MB per upload
    assert backend.MAX_QUEUED_PER_FRIEND == 2   # two songs waiting or splitting


def test_a_new_friend_gets_those_defaults(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QT_PASSCODE", "ownerkey1")
    f = tmp_path / "users.json"
    assert users.main(["add", "Cam"], users_file=f, stems_dir=tmp_path, library_dir=tmp_path) == 0
    person = users.load(f)[0]
    assert (person["quota_mb"], person["max_upload_mb"]) == (5000, 200)


def test_two_songs_at_a_time_and_the_queue_is_per_friend(fresh_friends):
    client = fresh_friends
    assert send(client, ALEX).status_code == 200
    assert send(client, ALEX).status_code == 200
    third = send(client, ALEX)
    assert third.status_code == 429
    assert send(client, BO).status_code == 200  # Alex being full does not block Bo
    backend.JOBS.clear()
    assert send(client, ALEX).status_code == 200  # a slot frees up once jobs finish


def test_finished_jobs_do_not_count_against_the_queue(fresh_friends):
    client = fresh_friends
    first, second = (send(client, ALEX).json()["job_id"] for _ in range(2))
    backend.JOBS[first]["status"] = "done"
    assert send(client, ALEX).status_code == 200
    assert second in backend.JOBS


def test_five_gigabytes_of_songs_is_the_ceiling(fresh_friends, monkeypatch):
    client = fresh_friends
    monkeypatch.setattr(users, "usage", lambda stems, lib, uid: (9, 4_999_999_999))  # one byte under 5 GB
    assert send(client, ALEX).status_code == 200
    backend.JOBS.clear()
    monkeypatch.setattr(users, "usage", lambda stems, lib, uid: (9, 5_000_000_000))  # exactly 5 GB: full
    full = send(client, ALEX)
    assert full.status_code == 403 and "full" in full.json()["detail"].lower()


def test_the_owner_has_no_friend_limits(two, monkeypatch):
    client, *_ = two
    monkeypatch.setattr(backend, "run_job", lambda *a, **k: None)
    assert all(send(client, OWN).status_code == 200 for _ in range(3))


def test_an_upload_over_200_mb_is_refused_and_200_mb_is_not(fresh_friends):
    client = fresh_friends
    too_big = send(client, ALEX, size=201 * MB)
    assert too_big.status_code == 413 and "200 MB" in too_big.json()["detail"]
    assert send(client, ALEX, size=200 * MB - 4096).status_code == 200  # a file just under the cap goes through
    assert backend.users.load(backend.USERS_FILE)[0]["max_upload_mb"] == 200


def test_each_friend_only_sees_their_own_library(fresh_friends):
    client = fresh_friends
    ids = {}
    for who, n in (("alex", 2), ("bo", 3), ("owner", 1)):
        ids[who] = []
        for i in range(n):
            sid = f"{len(sum(ids.values(), [])) + 1:010x}"
            make_song(client, sid, f"{who}-{i}")
            (backend.STEMS_DIR / sid / "meta.json").write_text(json.dumps({"title": f"{who}-{i}", "created": 1000 + i, "owner": who}))
            ids[who].append(sid)
    titles = lambda h: sorted(s["title"] for s in client.get("/api/library", headers=h).json()["songs"])  # noqa: E731
    assert titles(ALEX) == ["Alex's song", "alex-0", "alex-1"]  # (the fixture already holds one song each for Alex and the owner)
    assert titles(BO) == ["bo-0", "bo-1", "bo-2"]
    assert titles(OWN) == ["Mine", "owner-0"]
    for mine, theirs in (("alex", "bo"), ("bo", "alex"), ("alex", "owner"), ("bo", "owner")):
        headers = ALEX if mine == "alex" else BO
        for sid in ids[theirs]:
            assert client.get(f"/stems/{sid}/vocals.mp3", headers=headers).status_code == 404
            assert client.get(f"/api/analysis/{sid}", headers=headers).status_code == 404
            assert client.delete(f"/api/library/{sid}", headers=headers).status_code == 404
    assert client.post("/api/library/delete", json={"ids": ids["bo"]}, headers=ALEX).json() == {"deleted": []}
    assert titles(BO) == ["bo-0", "bo-1", "bo-2"]  # nothing Alex tried touched Bo's songs


def test_library_free_space_is_each_friends_own_allowance(fresh_friends):
    client = fresh_friends
    for h in (ALEX, BO):
        lib = client.get("/api/library", headers=h).json()
        assert lib["disk_free_bytes"] == 5_000_000_000 - lib["total_bytes"]
