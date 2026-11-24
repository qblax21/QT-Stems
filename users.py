"""People who may use this server, and the command-line tool that manages them.

You (the owner) always exist: your passcode is passcode.txt / QT_PASSCODE, and every song made before accounts existed
is yours. Friends live in users.json next to this file, each with their own passcode, which is stored only as a salted
argon2 hash: it is shown once when you create or reset it and can't be looked up afterwards. A person only ever sees
their own songs, sessions and downloads; the owner's backup and error pages are not available to friends.

    python users.py add "Alex"          create a friend and print the passcode to send them
    python users.py list                everyone and how much space they use (passcodes are never shown)
    python users.py reset alex          give alex a new passcode and print it once (the old one stops working at once)
    python users.py remove alex         cut alex off (add --delete-songs to also delete their songs)
"""
import argparse
import json
import os
import re
import secrets
import shutil
import sys
import time
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

OWNER_ID = "owner"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alike characters (0/o, 1/l/i)
KEY_LENGTH = 10  # 31**10 is roughly 8e14 guesses
DEFAULT_QUOTA_MB = 5000
DEFAULT_UPLOAD_MB = 200
BASE_DIR = Path(__file__).resolve().parent
_HASHER = PasswordHasher()


def hash_key(key: str) -> str:
    return _HASHER.hash(key)


def check_key(stored_hash: str, key: str) -> bool:
    try:
        return _HASHER.verify(stored_hash, key)
    except (VerificationError, InvalidHashError):
        return False


def new_key() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(KEY_LENGTH))


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-")[:32]


def load(path: Path) -> list:
    """Friends from users.json. Entries that are malformed (or try to be the owner) are ignored."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8")).get("users", [])
    except Exception:
        return []
    out, legacy = [], False
    for u in raw if isinstance(raw, list) else []:
        if not isinstance(u, dict):
            continue
        uid, key, hashed = str(u.get("id", "")), str(u.get("key", "")), str(u.get("hash", ""))
        if not ID_RE.match(uid) or uid == OWNER_ID:
            continue
        if not hashed.startswith("$argon2"):
            if len(key) < 6:
                continue
            hashed, legacy = hash_key(key), True  # an older file with the passcode in plain text: hash it now
        out.append({
            "id": uid, "name": str(u.get("name") or uid)[:60], "hash": hashed, "admin": False,
            "quota_mb": int(u.get("quota_mb") or DEFAULT_QUOTA_MB),
            "max_upload_mb": int(u.get("max_upload_mb") or DEFAULT_UPLOAD_MB),
            "created": u.get("created", 0),
        })
    if legacy:
        try:
            save(path, out)
        except OSError:
            pass
    return out


def save(path: Path, people: list) -> None:
    keep = ("id", "name", "hash", "quota_mb", "max_upload_mb", "created")
    rows = []
    for p in people:
        row = {k: p[k] for k in keep if k in p}
        if "hash" not in row and p.get("key"):  # callers may hand over a fresh plain passcode; only its hash is written
            row["hash"] = hash_key(p["key"])
        rows.append(row)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"users": rows}, indent=2), encoding="utf-8")
    tmp.replace(path)


def song_owner(song_dir: Path) -> str:
    """Whose song this is. Songs made before accounts existed have no owner field and belong to the owner."""
    try:
        owner = json.loads((song_dir / "meta.json").read_text(encoding="utf-8")).get("owner")
    except Exception:
        return OWNER_ID
    return owner if isinstance(owner, str) and owner else OWNER_ID


def song_bytes(song_dir: Path, library_dir: Path) -> int:
    try:
        total = sum(p.stat().st_size for p in song_dir.iterdir() if p.is_file())
        return total + sum(p.stat().st_size for p in library_dir.glob(song_dir.name + ".*"))
    except OSError:
        return 0


def usage(stems_dir: Path, library_dir: Path, uid: str):
    """(songs, bytes) belonging to uid."""
    n = size = 0
    if stems_dir.is_dir():
        for d in stems_dir.iterdir():
            if d.is_dir() and re.fullmatch(r"[0-9a-f]{10}", d.name) and song_owner(d) == uid:
                n += 1
                size += song_bytes(d, library_dir)
    return n, size


def owner_key() -> str:
    value = os.environ.get("QT_PASSCODE", "").strip()
    if value:
        return value
    try:
        return (BASE_DIR / "passcode.txt").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


# ---- command line -------------------------------------------------------------------------------------------

def _find(people, who):
    who = slug(who)
    return next((p for p in people if p["id"] == who), None)


def main(argv=None, users_file: Path = BASE_DIR / "users.json", stems_dir: Path = BASE_DIR / "stems", library_dir: Path = BASE_DIR / "library") -> int:
    ap = argparse.ArgumentParser(prog="users.py", description="Manage who can use Quick Tune.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="create a friend")
    a.add_argument("name")
    a.add_argument("--quota-gb", type=float, default=DEFAULT_QUOTA_MB / 1000, help="how much space their songs may use (default 5)")
    a.add_argument("--upload-mb", type=int, default=DEFAULT_UPLOAD_MB, help="largest single upload (default 200)")
    sub.add_parser("list", help="show everyone")
    r = sub.add_parser("reset", help="give someone a new passcode")
    r.add_argument("name")
    d = sub.add_parser("remove", help="cut someone off")
    d.add_argument("name")
    d.add_argument("--delete-songs", action="store_true", help="also delete their songs and downloads")
    args = ap.parse_args(argv)

    people = load(users_file)
    if args.cmd == "add":
        uid = slug(args.name)
        if not ID_RE.match(uid) or uid == OWNER_ID:
            print("Pick a name made of letters or numbers (and not 'owner').")
            return 1
        if _find(people, uid):
            print(f"{uid} already exists. Use 'reset {uid}' for a new passcode.")
            return 1
        if not owner_key():
            print("Set your own passcode first (put it in passcode.txt). With no passcode the server is open to everyone.")
            return 1
        key = new_key()
        people.append({"id": uid, "name": args.name.strip()[:60], "hash": hash_key(key), "quota_mb": int(args.quota_gb * 1000), "max_upload_mb": args.upload_mb, "created": time.time()})
        save(users_file, people)
        print(f"Created {args.name.strip()}.\nPasscode: {key}\nSend them the app address and this passcode now: it is not stored anywhere readable, so it can't be shown again (use 'reset' for a new one). It works immediately; nothing needs restarting.")
        return 0
    if args.cmd == "list":
        n, size = usage(stems_dir, library_dir, OWNER_ID)
        print(f"{'owner':<14}{n:>4} songs {size / 1e9:>6.2f} GB   (passcode.txt)")
        for p in people:
            n, size = usage(stems_dir, library_dir, p["id"])
            print(f"{p['id']:<14}{n:>4} songs {size / 1e9:>6.2f} GB of {p['quota_mb'] / 1000:.1f}   passcode set")
        return 0
    p = _find(people, args.name)
    if not p:
        print(f"Nobody called '{args.name}'. Run 'list' to see everyone.")
        return 1
    if args.cmd == "reset":
        key = new_key()
        p["hash"] = hash_key(key)
        save(users_file, people)
        print(f"New passcode for {p['id']}: {key}\nThe old one stopped working. Send it now; it can't be shown again.")
        return 0
    people = [x for x in people if x["id"] != p["id"]]
    save(users_file, people)
    gone = 0
    if args.delete_songs and stems_dir.is_dir():
        for sd in list(stems_dir.iterdir()):
            if sd.is_dir() and re.fullmatch(r"[0-9a-f]{10}", sd.name) and song_owner(sd) == p["id"]:
                shutil.rmtree(sd, ignore_errors=True)
                for f in library_dir.glob(sd.name + ".*"):
                    f.unlink(missing_ok=True)
                gone += 1
        (users_file.parent / f"sessions-{p['id']}.json").unlink(missing_ok=True)
    print(f"Removed {p['id']}; their passcode no longer works." + (f" Deleted {gone} songs." if args.delete_songs else " Their songs are still on disk (re-run with --delete-songs to delete them)."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
