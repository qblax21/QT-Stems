from pathlib import Path
from uuid import uuid4
import asyncio
import hmac
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback

from fastapi import FastAPI, File, Form, Request, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

import backup
import chords
import fidelity
import users
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Quick Tune Demucs Backend")


def _load_passcode() -> str:
    """Optional shared passcode: QT_PASSCODE env var, else the first line of passcode.txt. Empty = no lock."""
    value = os.environ.get("QT_PASSCODE", "").strip()
    if value:
        return value
    f = Path(__file__).resolve().parent / "passcode.txt"
    try:
        return f.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


PASSCODE = _load_passcode()
MAX_UPLOAD_MB = int(os.environ.get("QT_MAX_UPLOAD_MB", "500"))
OWNER = {"id": users.OWNER_ID, "name": "Owner", "admin": True}
ADMIN_PATHS = ("/api/backup", "/api/restore", "/api/last-error")  # the owner's own housekeeping, never for friends
MAX_FAILS, FAIL_WINDOW = 10, 600  # wrong passcodes allowed per address, per ten minutes
LOGIN_FAILS = {}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")  # the last entry is the one a trusted proxy (Tailscale) added
    return fwd.split(",")[-1].strip() or (request.client.host if request.client else "?")


def identify(key: str):
    """The person this passcode belongs to, or None. With no passcodes configured at all the server is open (the owner)."""
    friends = users.load(USERS_FILE)
    if not PASSCODE and not friends:
        return OWNER
    found = None
    for secret, person in ([(PASSCODE, OWNER)] if PASSCODE else []) + [(f["key"], f) for f in friends]:
        if hmac.compare_digest(key.encode(), secret.encode()):  # compare against everyone so timing reveals nothing
            found = person
    return found


def _user(request: Request) -> dict:
    return getattr(request.state, "user", OWNER)


# Registered before CORS so CORS ends up outermost and 401 responses still reach the browser.
@app.middleware("http")
async def require_passcode(request: Request, call_next):
    path = request.url.path
    if request.method != "OPTIONS" and path != "/api/health":
        key = request.headers.get("x-qt-key", "")
        ip, now = _client_ip(request), time.time()
        recent = [t for t in LOGIN_FAILS.get(ip, []) if now - t < FAIL_WINDOW]
        if len(recent) >= MAX_FAILS:
            return JSONResponse({"detail": "Too many wrong passcodes. Try again in a few minutes."}, status_code=429)
        user = identify(key)
        if user is None:
            if key:  # a request with no passcode at all is just the app asking; only real guesses count
                recent.append(now)
                LOGIN_FAILS[ip] = recent
                if len(LOGIN_FAILS) > 2000:
                    LOGIN_FAILS.clear()
            await asyncio.sleep(0.5)  # slow down guessing
            return JSONResponse({"detail": "Passcode required"}, status_code=401)
        request.state.user = user
        if not user["admin"] and path.startswith(ADMIN_PATHS):
            return JSONResponse({"detail": "Only the owner can do that."}, status_code=403)
        parts = path.split("/")
        if len(parts) > 2 and parts[1] == "stems" and JOB_ID_RE.match(parts[2]) and users.song_owner(STEMS_DIR / parts[2]) != user["id"]:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
    else:
        user = OWNER
    limit_mb = MAX_UPLOAD_MB if user["admin"] else min(MAX_UPLOAD_MB, user["max_upload_mb"])
    try:
        too_big = int(request.headers.get("content-length", "0")) > limit_mb * 1024 * 1024
    except ValueError:
        too_big = False
    if too_big:
        return JSONResponse({"detail": f"File is larger than {limit_mb} MB."}, status_code=413)
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Length", "Content-Range", "Accept-Ranges"],
)

BASE_DIR = Path(__file__).resolve().parent
STEMS_DIR = BASE_DIR / "stems"
STEMS_DIR.mkdir(parents=True, exist_ok=True)
ERROR_LOG = BASE_DIR / "last_error.txt"
LIBRARY_DIR = BASE_DIR / "library"  # original uploads, kept so every song sent from any device lives on this PC
LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_FILE = BASE_DIR / "sessions.json"
USERS_FILE = BASE_DIR / "users.json"  # friends and their passcodes; managed with users.py
BACKUP_DIR = BASE_DIR / "backups"
SESSIONS_LOCK = threading.Lock()
JOB_ID_RE = re.compile(r"^[0-9a-f]{10}$")
BASE_STEMS = {"vocals", "drums", "bass", "other", "guitar", "piano"}

ALLOWED_MODELS = {"htdemucs", "htdemucs_ft", "htdemucs_6s", "mdx_extra"}
TWO_STEM_CHOICES = {"vocals", "drums", "bass", "other", "guitar", "piano"}
SIX_STEM_ONLY = {"guitar", "piano"}
ALLOWED_DEVICES = {"cpu", "cuda"}
MP3_BITRATE = "256"

# Separation runs in a background thread, one job at a time. The browser polls /api/jobs/<id>.
JOBS = {}
RUN_LOCK = threading.Lock()
_current = {"job": None}
_hooked = False


def log_error(text: str):
    ERROR_LOG.write_text(text, encoding="utf-8")


def _install_progress_hooks():
    """Report real Demucs progress by counting finished audio chunks.

    Demucs only shows a tqdm bar *after* all chunks are computed on the default settings,
    so we hook apply_model instead: the top-level call works out how many chunks there
    will be, and every finished chunk bumps the job's percent.
    """
    global _hooked
    if _hooked:
        return
    import demucs.apply as da
    import demucs.separate as ds

    original = da.apply_model

    def leaf_hook(model, mix, *args, **kwargs):
        out = original(model, mix, *args, **kwargs)
        job = _current["job"]
        if job is not None and kwargs.get("split") is False and not kwargs.get("shifts"):
            job["_done"] += 1
            if job["_total"]:
                job["percent"] = round(min(99.0, job["_done"] / job["_total"] * 100), 1)
        return out

    def top_hook(model, mix, *args, **kwargs):
        job = _current["job"]
        if job is not None:
            try:
                overlap = kwargs.get("overlap", 0.25)
                length = mix.shape[-1]
                total = 0
                for m in list(getattr(model, "models", [model])):
                    seg = int(m.samplerate * m.segment)
                    stride = max(1, int((1 - overlap) * seg))
                    total += -(-length // stride)
                job["_total"], job["_done"] = max(1, total), 0
            except Exception:
                job["_total"], job["_done"] = 0, 0
        out = original(model, mix, *args, **kwargs)
        if job is not None:
            job["percent"] = 100.0
            job["stage"] = "saving"
        return out

    da.apply_model = leaf_hook
    ds.apply_model = top_hook
    _hooked = True


def run_demucs(input_path: Path, output_root: Path, model: str, device: str, stem: str = ""):
    try:
        from demucs.separate import main as demucs_main
    except Exception as e:
        raise RuntimeError(
            "Demucs is not installed. Run: python -m pip install -U demucs SoundFile fastapi uvicorn python-multipart"
        ) from e

    if str(device).startswith("cuda"):
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError(
                "CUDA was selected, but this environment does not have CUDA-enabled PyTorch."
            )

    try:
        _install_progress_hooks()
    except Exception:
        pass  # progress is cosmetic; never block a separation because of it

    args = [
        "-n", model,
        "-d", device,
        "--mp3", "--mp3-bitrate", MP3_BITRATE,
        "-o", str(output_root),
    ]
    if stem:
        args += ["--two-stems", stem]  # writes <stem> and no_<stem> only
    args.append(str(input_path))

    code = demucs_main(args)
    if code not in (0, None):
        raise RuntimeError(f"Demucs exited with code {code}")


def collect_stem_files(source_path: Path, output_root: Path, model: str):
    track_name = source_path.stem
    stem_dir = output_root / model / track_name

    if not stem_dir.exists():
        raise RuntimeError(f"Stem directory not found: {stem_dir}")

    allowed_stems = BASE_STEMS | {"no_" + b for b in BASE_STEMS}
    allowed_exts = {".wav", ".mp3", ".flac"}

    files = []
    for p in stem_dir.iterdir():
        if p.is_file() and p.stem.lower() in allowed_stems and p.suffix.lower() in allowed_exts:
            files.append(p)

    if not files:
        raise RuntimeError(f"No separated stem audio files found inside: {stem_dir}")

    files.sort(key=lambda p: p.stem.lower())
    return files


def run_job(job_id: str, source_path: Path, work_dir: Path, model: str, device: str, stem: str = "", title: str = "", owner: str = users.OWNER_ID):
    job = JOBS[job_id]
    try:
        with RUN_LOCK:  # one separation at a time; others stay "queued"
            _current["job"] = job
            job.update(status="running", stage="separate", percent=0.0, started=time.time())

            run_demucs(source_path, work_dir, model, device, stem)
            job.update(stage="saving", percent=100.0)

            stem_files = collect_stem_files(source_path, work_dir, model)

            public_dir = STEMS_DIR / job_id
            public_dir.mkdir(parents=True, exist_ok=True)
            try:  # first, so the song is theirs from the moment its files exist
                (public_dir / "meta.json").write_text(json.dumps({
                    "title": title or source_path.stem, "model": model, "device": device, "stem": stem,
                    "created": time.time(), "original": source_path.name, "owner": owner,
                }), encoding="utf-8")
            except Exception:
                log_error(traceback.format_exc())

            outputs = {}
            for stem_file in stem_files:
                target = public_dir / stem_file.name
                shutil.copy2(stem_file, target)
                outputs[stem_file.stem.lower()] = f"/stems/{job_id}/{target.name}"

            if not outputs:
                raise RuntimeError("Demucs finished, but no output audio files were copied.")

            try:  # library bookkeeping must never fail a finished separation
                shutil.copy2(source_path, LIBRARY_DIR / f"{job_id}{source_path.suffix.lower()}")
            except Exception:
                log_error(traceback.format_exc())

            job.update(status="done", stage="done", percent=100.0, outputs=outputs)
        threading.Thread(target=_detail_quietly, args=(job_id,), daemon=True).start()  # restore the original's detail in the background
    except BaseException as e:  # Demucs calls sys.exit() on some failures
        log_error(f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}")
        job.update(status="error", stage="error", error=str(e) or type(e).__name__)
    finally:
        _current["job"] = None
        shutil.rmtree(work_dir, ignore_errors=True)


DETAIL_LOCK = threading.Lock()


def ensure_detail(song_id: str) -> dict:
    """Create stems/<id>/detail.mp3 (original minus the stems) once. Never raises for 'cannot do it' cases."""
    song_dir = STEMS_DIR / song_id
    out, skip = song_dir / "detail.mp3", song_dir / "detail.skip"
    if out.exists():
        return {"status": "exists"}
    if skip.exists():
        return {"status": "skipped", "reason": skip.read_text(encoding="utf-8")[:200]}
    original = next((p for p in LIBRARY_DIR.glob(song_id + ".*")), None)
    ffmpeg = shutil.which("ffmpeg")
    if original is None or not ffmpeg:
        return {"status": "no-original"}
    with DETAIL_LOCK:
        if out.exists():
            return {"status": "exists"}
        try:
            info = fidelity.build_detail(song_dir, original, out, ffmpeg)
        except fidelity.Mismatch as e:
            skip.write_text(str(e), encoding="utf-8")
            return {"status": "skipped", "reason": str(e)}
        except Exception:
            log_error(traceback.format_exc())
            return {"status": "failed"}
    return {"status": "ready", **info}


def _detail_quietly(song_id: str):
    try:
        ensure_detail(song_id)
    except Exception:
        log_error(traceback.format_exc())


def own_song(request: Request, song_id: str) -> Path:
    """The folder of one of this person's songs; anyone else's song looks exactly like a song that doesn't exist."""
    d = STEMS_DIR / song_id
    if not JOB_ID_RE.match(song_id) or not d.is_dir() or users.song_owner(d) != _user(request)["id"]:
        raise HTTPException(status_code=404, detail="Unknown song")
    return d


@app.post("/api/detail/{song_id}")
def make_detail(request: Request, song_id: str):
    own_song(request, song_id)
    return ensure_detail(song_id)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "quick-tune-demucs", "device": "self-hosted", "auth": bool(PASSCODE or users.load(USERS_FILE))}


@app.get("/api/auth-check")
def auth_check(request: Request):
    u = _user(request)  # reaching this means the passcode (if any) was accepted
    return {"ok": True, "user": {"id": u["id"], "name": u["name"], "admin": u["admin"]}}


@app.get("/api/last-error")
def last_error():
    if ERROR_LOG.exists():
        return {"error": ERROR_LOG.read_text(encoding="utf-8")}
    return {"error": "No logged error yet."}


MAX_QUEUED_PER_FRIEND = 2


def check_friend_limits(user: dict):
    """Friends share this PC's graphics card and disk, so each gets a small queue and a storage allowance."""
    busy = sum(1 for j in JOBS.values() if j.get("_owner") == user["id"] and j["status"] in ("queued", "running"))
    if busy >= MAX_QUEUED_PER_FRIEND:
        raise HTTPException(status_code=429, detail="You already have songs waiting to be split. Wait for one to finish.")
    _, used = users.usage(STEMS_DIR, LIBRARY_DIR, user["id"])
    if used >= user["quota_mb"] * 1_000_000:
        raise HTTPException(status_code=403, detail="Your storage on this PC is full. Delete some songs from your Library first.")


@app.post("/api/demucs/separate")
def separate(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form("htdemucs"),
    device: str = Form("cpu"),
    stem: str = Form(""),
    title: str = Form(""),
):
    if model not in ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model: {model}")
    if device not in ALLOWED_DEVICES:
        raise HTTPException(status_code=400, detail=f"Unknown device: {device}")
    stem = (stem or "").strip().lower()
    if stem and stem not in TWO_STEM_CHOICES:
        raise HTTPException(status_code=400, detail=f"Unknown stem: {stem}")
    if stem in SIX_STEM_ONLY and model != "htdemucs_6s":
        raise HTTPException(status_code=400, detail=f"'{stem}' needs the 6-stem model (htdemucs_6s).")

    user = _user(request)
    if not user["admin"]:
        check_friend_limits(user)
    limit_mb = MAX_UPLOAD_MB if user["admin"] else min(MAX_UPLOAD_MB, user["max_upload_mb"])

    job_id = uuid4().hex[:10]
    work_dir = Path(tempfile.mkdtemp(prefix="quicktune_"))
    source_path = work_dir / Path(file.filename or "upload.wav").name

    try:
        written = 0
        with source_path.open("wb") as f:  # also enforced while copying, for uploads that don't announce their size
            while chunk := file.file.read(1 << 20):
                written += len(chunk)
                if written > limit_mb * 1024 * 1024:
                    raise HTTPException(status_code=413, detail=f"File is larger than {limit_mb} MB.")
                f.write(chunk)
    except HTTPException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        log_error(f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

    JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "stage": "queue",
        "percent": 0.0,
        "model": model,
        "device": device,
        "stem": stem,
        "outputs": {},
        "error": None,
        "_total": 0,
        "_done": 0,
        "_owner": user["id"],
    }
    threading.Thread(
        target=run_job, args=(job_id, source_path, work_dir, model, device, stem, title.strip()[:200], user["id"]), daemon=True
    ).start()
    return {"status": "queued", "job_id": job_id, "model": model, "device": device}


@app.get("/api/jobs/{job_id}")
def job_status(request: Request, job_id: str):
    job = JOBS.get(job_id)
    if job and job.get("_owner") != _user(request)["id"]:
        job = None
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job (the server may have restarted).")
    return {k: v for k, v in job.items() if not k.startswith("_")}


@app.get("/api/library")
def library(request: Request):
    """Every song this person has split on this PC (from any of their devices), newest first."""
    user = _user(request)
    items = []
    for d in STEMS_DIR.iterdir():
        if not d.is_dir() or not JOB_ID_RE.match(d.name) or users.song_owner(d) != user["id"]:
            continue
        meta = {}
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        outputs = {
            p.stem.lower(): f"/stems/{d.name}/{p.name}"
            for p in d.iterdir()
            if p.is_file() and p.suffix.lower() in {".wav", ".mp3", ".flac"}
            and p.stem.lower() in (BASE_STEMS | {"no_" + b for b in BASE_STEMS} | {"detail"})
        }
        if not outputs:
            continue
        size = sum(p.stat().st_size for p in d.iterdir() if p.is_file()) + sum(p.stat().st_size for p in LIBRARY_DIR.glob(d.name + ".*"))
        items.append({
            "id": d.name,
            "bytes": size,
            "title": meta.get("title") or "Untitled song",
            "model": meta.get("model", ""),
            "stem": meta.get("stem", ""),
            "created": meta.get("created") or d.stat().st_mtime,
            "outputs": outputs,
            "hasOriginal": any(LIBRARY_DIR.glob(d.name + ".*")),
        })
    items.sort(key=lambda i: i["created"], reverse=True)
    total = sum(i["bytes"] for i in items)
    free = shutil.disk_usage(BASE_DIR).free
    if not user["admin"]:
        free = min(free, max(0, user["quota_mb"] * 1_000_000 - total))  # what a friend sees as "space left" is their allowance
    return {"songs": items, "total_bytes": total, "disk_free_bytes": free}


@app.post("/api/library/delete")
def delete_songs(request: Request, payload: dict):
    ids = payload.get("ids")
    if not isinstance(ids, list) or len(ids) > 500:
        raise HTTPException(status_code=400, detail="Expected a list of ids")
    deleted = []
    for song_id in ids:
        if isinstance(song_id, str) and JOB_ID_RE.match(song_id) and users.song_owner(STEMS_DIR / song_id) == _user(request)["id"]:
            shutil.rmtree(STEMS_DIR / song_id, ignore_errors=True)
            for f in LIBRARY_DIR.glob(song_id + ".*"):
                f.unlink(missing_ok=True)
            JOBS.pop(song_id, None)
            deleted.append(song_id)
    return {"deleted": deleted}


@app.patch("/api/library/{song_id}")
def rename_song(request: Request, song_id: str, payload: dict):
    own_song(request, song_id)
    title = str(payload.get("title", "")).strip()[:200]
    if not title:
        raise HTTPException(status_code=400, detail="Title is empty")
    meta_file = STEMS_DIR / song_id / "meta.json"
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception:
        meta = {"created": (STEMS_DIR / song_id).stat().st_mtime}
    meta["title"] = title
    meta_file.write_text(json.dumps(meta), encoding="utf-8")
    return {"id": song_id, "title": title}


@app.delete("/api/library/{song_id}")
def delete_song(request: Request, song_id: str):
    if not JOB_ID_RE.match(song_id):
        raise HTTPException(status_code=400, detail="Bad id")
    if (STEMS_DIR / song_id).is_dir() and users.song_owner(STEMS_DIR / song_id) != _user(request)["id"]:
        raise HTTPException(status_code=404, detail="Unknown song")
    shutil.rmtree(STEMS_DIR / song_id, ignore_errors=True)
    for f in LIBRARY_DIR.glob(song_id + ".*"):
        f.unlink(missing_ok=True)
    JOBS.pop(song_id, None)
    return {"deleted": song_id}


ANALYSIS_LOCK = threading.Lock()


@app.get("/api/analysis/{song_id}")
def analysis(request: Request, song_id: str, refresh: int = 0):
    """Key and chord progression for a song, estimated from its stems and cached next to them."""
    song_dir = own_song(request, song_id)
    cache = song_dir / "chords.json"
    with ANALYSIS_LOCK:  # one analysis at a time keeps the PC responsive while Demucs may be running
        if not refresh:
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
                if data.get("version") == chords.VERSION:
                    return data
            except Exception:
                pass
        try:
            data = chords.analyze_song(song_dir)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except Exception as e:
            log_error(traceback.format_exc())
            raise HTTPException(status_code=500, detail=f"Could not analyze this song: {str(e)[:200]}")
        cache.write_text(json.dumps(data), encoding="utf-8")
        return data


def _tempo_chain(t: float):
    """atempo only accepts 0.5-2.0 per stage, so split larger changes into several stages."""
    stages = []
    while t > 2.0:
        stages.append(2.0); t /= 2.0
    while t < 0.5:
        stages.append(0.5); t /= 0.5
    stages.append(t)
    return ",".join(f"atempo={s:.5f}" for s in stages)


@app.post("/api/export")
def export_mix(request: Request, payload: dict):
    """Render the current mix (per-stem gains, speed, key) to an MP3 so it can be saved or shared."""
    song_id = str(payload.get("song_id", ""))
    song_dir = own_song(request, song_id)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(status_code=500, detail="ffmpeg is not installed on the PC.")
    try:
        speed = min(2.0, max(0.5, float(payload.get("speed", 1.0))))
        semis = min(12.0, max(-12.0, float(payload.get("pitch", 0.0))))
        gains = payload.get("gains") or {}
        names = BASE_STEMS | {"no_" + b for b in BASE_STEMS} | {"detail"}
        files = []
        for name, g in gains.items():
            g = min(2.0, max(0.0, float(g)))
            if name in names and g > 0.001:
                match = next((p for p in song_dir.iterdir() if p.stem == name and p.suffix.lower() in {".mp3", ".wav", ".flac"}), None)
                if match:
                    files.append((match, g))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Bad export request")
    if not files:
        raise HTTPException(status_code=400, detail="Nothing audible to export (every stem is muted).")

    sr = 44100
    pitch = 2 ** (semis / 12)
    parts = [f"[{i}:a]volume={g:.3f}[a{i}]" for i, (_, g) in enumerate(files)]
    mix = "".join(f"[a{i}]" for i in range(len(files)))
    chain = f"{mix}amix=inputs={len(files)}:normalize=0:duration=longest"
    if abs(pitch - 1) > 1e-6 or abs(speed - 1) > 1e-6:
        chain += f",asetrate={sr * pitch:.2f},aresample={sr},{_tempo_chain(speed / pitch)}"
    chain += ",alimiter=limit=0.95[out]"
    fd, out_name = tempfile.mkstemp(prefix="quicktune_mix_", suffix=".mp3")
    os.close(fd)  # keep the file but release the handle, or Windows can't delete it afterwards
    out = Path(out_name)
    cmd = [ffmpeg, "-y", "-v", "error"]
    for p, _ in files:
        cmd += ["-i", str(p)]
    cmd += ["-filter_complex", ";".join(parts + [chain]), "-map", "[out]", "-ar", str(sr), "-b:a", "192k", str(out)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        out.unlink(missing_ok=True)
        log_error(result.stderr)
        raise HTTPException(status_code=500, detail="ffmpeg could not render the mix.")
    title = "mix"
    try:
        title = json.loads((song_dir / "meta.json").read_text(encoding="utf-8")).get("title") or title
    except Exception:
        pass
    safe = re.sub(r'[\\/:*?"<>|]+', "_", title).strip()[:80] or "mix"
    return FileResponse(out, media_type="audio/mpeg", filename=f"{safe} (QT mix).mp3", background=BackgroundTask(out.unlink, missing_ok=True))


def sessions_file(request: Request) -> Path:
    uid = _user(request)["id"]
    return SESSIONS_FILE if uid == users.OWNER_ID else SESSIONS_FILE.with_name(f"sessions-{uid}.json")


@app.get("/api/sessions")
def get_sessions(request: Request):
    with SESSIONS_LOCK:
        try:
            data = json.loads(sessions_file(request).read_text(encoding="utf-8"))
            return {"sessions": data.get("sessions", []), "deleted": data.get("deleted", [])}
        except Exception:
            return {"sessions": [], "deleted": []}


@app.put("/api/sessions")
def put_sessions(request: Request, payload: dict):
    sessions = payload.get("sessions")
    if not isinstance(sessions, list) or len(sessions) > 500:
        raise HTTPException(status_code=400, detail="Expected a list of sessions")
    deleted = payload.get("deleted") or []
    if not isinstance(deleted, list):
        deleted = []
    text = json.dumps({"sessions": sessions, "deleted": [str(d) for d in deleted][-1000:]})
    if len(text) > 5_000_000:
        raise HTTPException(status_code=413, detail="Too large")
    with SESSIONS_LOCK:
        sessions_file(request).write_text(text, encoding="utf-8")
    return {"saved": len(sessions)}


app.mount("/stems", StaticFiles(directory=STEMS_DIR), name="stems")


# ---- backups ---------------------------------------------------------------------------------------------------

@app.post("/api/backup")
def make_backup(payload: dict):
    """Write a backup zip into the backups folder and send it back so the device can keep a copy too."""
    client = payload.get("client")
    client = client if isinstance(client, dict) else None
    with SESSIONS_LOCK:
        path = backup.make_backup(BACKUP_DIR, STEMS_DIR, SESSIONS_FILE, client)
    return FileResponse(path, media_type="application/zip", filename=path.name)


@app.get("/api/backup/status")
def backup_status():
    items = backup.list_backups(BACKUP_DIR)
    return {"count": len(items), "last": items[0]["created"] if items else None, "folder": str(BACKUP_DIR), "items": items[:5]}


@app.post("/api/restore")
async def restore_backup(file: UploadFile = File(...)):
    data = await file.read()
    try:
        with SESSIONS_LOCK:
            backup.make_backup(BACKUP_DIR, STEMS_DIR, SESSIONS_FILE, None, prefix="qt-before-restore")  # undo point
            return backup.restore(data, STEMS_DIR, SESSIONS_FILE)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _auto_backup_loop():
    """Once a day, if nothing newer exists, save a server-side backup (sessions, titles, chords)."""
    while True:
        try:
            items = backup.list_backups(BACKUP_DIR)
            if not items or time.time() - items[0]["created"] > 22 * 3600:
                with SESSIONS_LOCK:
                    backup.make_backup(BACKUP_DIR, STEMS_DIR, SESSIONS_FILE, None, prefix="qt-auto")
        except Exception:
            log_error(traceback.format_exc())
        time.sleep(3600)


@app.on_event("startup")
def start_auto_backup():
    if os.environ.get("QT_AUTOBACKUP", "1") != "0":
        threading.Thread(target=_auto_backup_loop, daemon=True).start()
