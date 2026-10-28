<p align="center"><img src="logo.svg" alt="QT Stems logo" width="96"></p>

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
- `htdemucs_ft`: same 4 stems, cleaner separation, about 4x slower
- `mdx_extra`: alternative 4-stem model (API only, not in the app)

"What to split" in the app runs Demucs in two-stem mode: you get just the chosen part (vocals, drums, bass, guitar or piano) plus a single backing track. Guitar and piano switch to the 6-stem model automatically. Demucs does not split beyond 6 stems.

Pick GPU in the app only if CUDA-enabled PyTorch is installed (`python -c "import torch; print(torch.cuda.is_available())"`).

### Start automatically at login (Windows)
`run-servers.ps1` keeps both servers running and restarts them if they stop. Register it once in PowerShell:
```powershell
Register-ScheduledTask -TaskName QuickTuneServers -User $env:USERNAME -RunLevel Limited -Action (New-ScheduledTaskAction -Execute powershell.exe -Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\path\to\QT-Stems\run-servers.ps1"') -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable)
```
Remove it with `Unregister-ScheduledTask QuickTuneServers`. Set `QT_PYTHON` to your python.exe if it is not on PATH.

## How it works
- The app uploads a song, then polls `/api/jobs/<id>` for real separation progress (Demucs runs in a background thread, one job at a time).
- Stems are saved as 256 kbps MP3 and downloaded fully to the device before the practice controls unlock.
- Playback uses Web Audio: every stem is decoded in memory and mixed inside one AudioWorklet, so all stems share a single sample clock (no drift), mute/solo/seek are instant, and A/B loops wrap sample-accurately. Speed changes use WSOLA time-stretching, so pitch is preserved. Very long songs are decoded at a lower sample rate to stay within phone memory.

- On plain `http` (a phone on your LAN) browsers hide AudioWorklet, so the same mixer runs on a `ScriptProcessorNode` instead. The line under "Stem mixer" shows which one is active.
- Saved sessions (song, loop, speed, mix, notes) and the last song you practiced are kept in the browser's local storage, with stem addresses stored relative to the backend so they work from the PC, LAN or Tailscale. The theme is purple and silver, with light and dark modes (follows your system, toggle in the header).

- It is installable: on iPhone use Share, then Add to Home Screen, and it opens full screen with the QT Stems icon (`icons/` holds the PNGs, `manifest.webmanifest` the Android/Chrome install info).

- **Library:** every song split from any device is stored on the PC: the stems under `stems/` and the original upload under `library/`. The Library tab lists, searches, renames and deletes them, so you can practice a song from your phone or computer without uploading it again. Saved sessions are also kept on the PC (`sessions.json`) and shared between devices; the browser keeps a local copy as a fallback.

## Security note
This is a local/LAN tool. There is no authentication, upload limit or job queue, and `python -m http.server` serves the whole folder. Do not expose it to the public internet as-is. Separated stems are saved under `stems/`.

## Troubleshooting
- Failures are written to `last_error.txt` and shown at `/api/last-error`.
- `favicon.ico` 404 and `GET /` 404 on the backend are harmless.

## License
MIT
