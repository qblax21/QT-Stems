from pathlib import Path
from uuid import uuid4
import shutil
import tempfile
import threading
import time
import traceback

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Quick Tune Demucs Backend")
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

ALLOWED_MODELS = {"htdemucs", "htdemucs_6s", "mdx_extra"}
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


def run_demucs(input_path: Path, output_root: Path, model: str, device: str):
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
        str(input_path),
    ]

    code = demucs_main(args)
    if code not in (0, None):
        raise RuntimeError(f"Demucs exited with code {code}")


def collect_stem_files(source_path: Path, output_root: Path, model: str):
    track_name = source_path.stem
    stem_dir = output_root / model / track_name

    if not stem_dir.exists():
        raise RuntimeError(f"Stem directory not found: {stem_dir}")

    allowed_stems = {"vocals", "drums", "bass", "other", "guitar", "piano", "no_vocals"}
    allowed_exts = {".wav", ".mp3", ".flac"}

    files = []
    for p in stem_dir.iterdir():
        if p.is_file() and p.stem.lower() in allowed_stems and p.suffix.lower() in allowed_exts:
            files.append(p)

    if not files:
        raise RuntimeError(f"No separated stem audio files found inside: {stem_dir}")

    files.sort(key=lambda p: p.stem.lower())
    return files


def run_job(job_id: str, source_path: Path, work_dir: Path, model: str, device: str):
    job = JOBS[job_id]
    try:
        with RUN_LOCK:  # one separation at a time; others stay "queued"
            _current["job"] = job
            job.update(status="running", stage="separate", percent=0.0, started=time.time())

            run_demucs(source_path, work_dir, model, device)
            job.update(stage="saving", percent=100.0)

            stem_files = collect_stem_files(source_path, work_dir, model)

            public_dir = STEMS_DIR / job_id
            public_dir.mkdir(parents=True, exist_ok=True)

            outputs = {}
            for stem_file in stem_files:
                target = public_dir / stem_file.name
                shutil.copy2(stem_file, target)
                outputs[stem_file.stem.lower()] = f"/stems/{job_id}/{target.name}"

            if not outputs:
                raise RuntimeError("Demucs finished, but no output audio files were copied.")

            job.update(status="done", stage="done", percent=100.0, outputs=outputs)
    except BaseException as e:  # Demucs calls sys.exit() on some failures
        log_error(f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}")
        job.update(status="error", stage="error", error=str(e) or type(e).__name__)
    finally:
        _current["job"] = None
        shutil.rmtree(work_dir, ignore_errors=True)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "quick-tune-demucs", "device": "self-hosted"}


@app.get("/api/last-error")
def last_error():
    if ERROR_LOG.exists():
        return {"error": ERROR_LOG.read_text(encoding="utf-8")}
    return {"error": "No logged error yet."}


@app.post("/api/demucs/separate")
def separate(
    file: UploadFile = File(...),
    model: str = Form("htdemucs"),
    device: str = Form("cpu"),
):
    if model not in ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown model: {model}")
    if device not in ALLOWED_DEVICES:
        raise HTTPException(status_code=400, detail=f"Unknown device: {device}")

    job_id = uuid4().hex[:10]
    work_dir = Path(tempfile.mkdtemp(prefix="quicktune_"))
    source_path = work_dir / Path(file.filename or "upload.wav").name

    try:
        with source_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)
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
        "outputs": {},
        "error": None,
        "_total": 0,
        "_done": 0,
    }
    threading.Thread(
        target=run_job, args=(job_id, source_path, work_dir, model, device), daemon=True
    ).start()
    return {"status": "queued", "job_id": job_id, "model": model, "device": device}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job (the server may have restarted).")
    return {k: v for k, v in job.items() if not k.startswith("_")}


app.mount("/stems", StaticFiles(directory=STEMS_DIR), name="stems")
