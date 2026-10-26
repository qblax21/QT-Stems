# QT-Stems (Quick Tune)

A self-hosted music practice app. Upload a song, split it into stems with [Demucs](https://github.com/facebookresearch/demucs), then mix stems, mute the instrument you want to play, loop sections, change speed and keep practice notes. Runs on your own computer; no paid service needed. Use it from your phone over your home Wi-Fi.

## Requirements
- Python 3.10-3.11 and ffmpeg
- PyTorch (CPU works, an NVIDIA GPU with CUDA is much faster)

## Setup
```bash
python -m venv venv
venv\Scripts\activate          # Windows (use `source venv/bin/activate` on macOS/Linux)
# Install PyTorch first: pick CPU or CUDA at https://pytorch.org/get-started/locally/
pip install -r requirements.txt
```

## Run
Terminal 1, backend:
```bash
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```
Terminal 2, frontend:
```bash
python -m http.server 5500 --bind 0.0.0.0
```
Open `http://localhost:5500/practice-cover-app-v7.html`.

### From your phone
1. Find your PC's IPv4 address (`ipconfig` on Windows).
2. On the phone (same Wi-Fi) open `http://<PC-IP>:5500/practice-cover-app-v7.html`. The backend address fills in automatically as `http://<PC-IP>:8000`.
3. Settings, then Check Backend, should say ok.
4. If it can't connect, allow Python through Windows Firewall for private networks only.

## Models
- `htdemucs`: 4 stems (vocals, drums, bass, other)
- `htdemucs_6s`: adds guitar and piano
- `mdx_extra`: alternative 4-stem model

Pick GPU in the app only if CUDA-enabled PyTorch is installed (`python -c "import torch; print(torch.cuda.is_available())"`).

## Security note
This is a local/LAN tool. There is no authentication, upload limit or job queue, and `python -m http.server` serves the whole folder. Do not expose it to the public internet as-is. Separated stems are saved under `stems/`.

## Troubleshooting
- Failures are written to `last_error.txt` and shown at `/api/last-error`.
- `favicon.ico` 404 and `GET /` 404 on the backend are harmless.

## License
MIT
