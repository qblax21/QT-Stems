#!/usr/bin/env bash
# One-time setup on macOS/Linux:  ./setup.sh        (CPU PyTorch)   or   ./setup.sh cuda   (NVIDIA GPU)
set -euo pipefail
cd "$(dirname "$0")"
command -v ffmpeg >/dev/null || echo "Warning: ffmpeg is not installed (brew install ffmpeg / apt install ffmpeg)."
PY=$(command -v python3.11 || command -v python3.10 || command -v python3)
[ -d venv ] || "$PY" -m venv venv
./venv/bin/python -m pip install --upgrade pip
if [ "${1:-}" = "cuda" ]; then ./venv/bin/python -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu118
else ./venv/bin/python -m pip install torch torchaudio; fi
./venv/bin/python -m pip install -r requirements.txt
if [ ! -f passcode.txt ]; then
  ./venv/bin/python -c "import secrets;a='abcdefghjkmnpqrstuvwxyz23456789';print('-'.join(''.join(secrets.choice(a) for _ in range(4)) for _ in range(3)))" > passcode.txt
  echo "Created passcode.txt (the app asks for it): $(cat passcode.txt)"
fi
echo
echo "Done. Run: ./venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8000"
echo "and:      ./venv/bin/python web.py 5500"
