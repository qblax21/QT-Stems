"""Backups of everything that can't be re-made: sessions, song names, chord analyses and the browser-side
settings (layout, practice log, per-song practice state). The song audio is not included; it can be split again."""
import io
import json
import re
import time
import zipfile
from pathlib import Path

KEEP = 14
SONG_ID = r"[0-9a-f]{10}"
ALLOWED = re.compile(rf"^(manifest\.json|sessions\.json|client\.json|songs/{SONG_ID}/(meta|chords)\.json)$")
CLIENT_KEYS = ("qt.layout", "qt.history", "qt.songs", "qt.sessions", "qt.deleted", "qt.theme", "qt.chordNumerals")
MAX_RESTORE_BYTES = 60_000_000


def _stamp(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}.zip"


def list_backups(backup_dir: Path):
    files = sorted(backup_dir.glob("qt-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True) if backup_dir.is_dir() else []
    return [{"name": p.name, "bytes": p.stat().st_size, "created": p.stat().st_mtime} for p in files]


def make_backup(backup_dir: Path, stems_dir: Path, sessions_file: Path, client: dict | None = None, prefix: str = "qt-backup") -> Path:
    backup_dir.mkdir(exist_ok=True)
    out = backup_dir / _stamp(prefix)
    n = 1
    while out.exists():  # two backups inside one second
        out = backup_dir / _stamp(prefix).replace(".zip", f"-{n}.zip"); n += 1
    songs = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        if sessions_file.is_file():
            z.write(sessions_file, "sessions.json")
        if stems_dir.is_dir():
            for d in sorted(stems_dir.iterdir()):
                if d.is_dir() and re.fullmatch(SONG_ID, d.name):
                    for name in ("meta.json", "chords.json"):
                        if (d / name).is_file():
                            z.write(d / name, f"songs/{d.name}/{name}")
                    songs += 1
        if client:
            z.writestr("client.json", json.dumps({k: v for k, v in client.items() if k in CLIENT_KEYS}))
        z.writestr("manifest.json", json.dumps({"app": "qt-stems", "format": 1, "created": time.time(), "songs": songs}))
    for old in list_backups(backup_dir)[KEEP:]:
        (backup_dir / old["name"]).unlink(missing_ok=True)
    return out


def restore(data: bytes, stems_dir: Path, sessions_file: Path) -> dict:
    """Apply a backup zip. Only whitelisted files are read, and song files only for songs that still exist here.
    Returns {'client': {...}, 'sessions': bool, 'songs': n}. Raises ValueError for anything that isn't a valid backup."""
    if len(data) > MAX_RESTORE_BYTES:
        raise ValueError("That file is too large to be a backup.")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("That is not a backup file.")
    names = [n for n in z.namelist() if ALLOWED.match(n)]
    if "manifest.json" not in names:
        raise ValueError("That is not a Quick Tune backup.")

    def load(name):
        info = z.getinfo(name)
        if info.file_size > 20_000_000:
            raise ValueError("The backup contains an oversized file.")
        return json.loads(z.read(name).decode("utf-8"))

    try:
        if load("manifest.json").get("app") != "qt-stems":
            raise ValueError("That is not a Quick Tune backup.")
        result = {"client": {}, "sessions": False, "songs": 0}
        if "client.json" in names:
            client = load("client.json")
            if isinstance(client, dict):
                result["client"] = {k: v for k, v in client.items() if k in CLIENT_KEYS}
        if "sessions.json" in names:
            s = load("sessions.json")
            if isinstance(s, dict) and isinstance(s.get("sessions"), list):
                sessions_file.write_text(json.dumps(s), encoding="utf-8")
                result["sessions"] = True
        for name in names:
            m = re.fullmatch(rf"songs/({SONG_ID})/(meta|chords)\.json", name)
            if m and (stems_dir / m.group(1)).is_dir():
                obj = load(name)
                if isinstance(obj, dict):
                    (stems_dir / m.group(1) / f"{m.group(2)}.json").write_text(json.dumps(obj), encoding="utf-8")
                    result["songs"] += m.group(2) == "meta"
        return result
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("The backup file is damaged.")
