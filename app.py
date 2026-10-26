from pathlib import Path
from uuid import uuid4
import shutil
import tempfile
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
)

BASE_DIR = Path(__file__).resolve().parent
STEMS_DIR = BASE_DIR / "stems"
STEMS_DIR.mkdir(parents=True, exist_ok=True)
ERROR_LOG = BASE_DIR / "last_error.txt"


def log_error(text: str):
    ERROR_LOG.write_text(text, encoding="utf-8")


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

    args = [
        "-n", model,
        "-d", device,
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


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "quick-tune-demucs", "device": "self-hosted"}


@app.get("/api/last-error")
def last_error():
    if ERROR_LOG.exists():
        return {"error": ERROR_LOG.read_text(encoding="utf-8")}
    return {"error": "No logged error yet."}


@app.post("/api/demucs/separate")
async def separate(
    file: UploadFile = File(...),
    model: str = Form("htdemucs"),
    device: str = Form("cpu"),
):
    job_id = uuid4().hex[:10]
    work_dir = Path(tempfile.mkdtemp(prefix="quicktune_"))

    safe_name = Path(file.filename or "upload.wav").name
    source_path = work_dir / safe_name

    try:
        with source_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)

        run_demucs(source_path, work_dir, model, device)

        stem_files = collect_stem_files(source_path, work_dir, model)

        public_dir = STEMS_DIR / job_id
        public_dir.mkdir(parents=True, exist_ok=True)

        outputs = {}
        for stem_file in stem_files:
            target = public_dir / stem_file.name
            shutil.copy2(stem_file, target)

            key = stem_file.stem.lower()
            outputs[key] = f"/stems/{job_id}/{target.name}"

        if not outputs:
            raise RuntimeError("Demucs finished, but no output audio files were copied.")

        return {
            "status": "completed",
            "job_id": job_id,
            "model": model,
            "device": device,
            "outputs": outputs,
        }

    except HTTPException:
        raise
    except Exception as e:
        text = f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"
        log_error(text)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


app.mount("/stems", StaticFiles(directory=STEMS_DIR), name="stems")