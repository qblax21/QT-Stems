<p align="center"><img src="logo.svg" alt="QT Stems logo" width="96"></p>

# QT-Stems (Quick Tune)

A self-hosted music practice app. Upload a song, split it into stems with [Demucs](https://github.com/facebookresearch/demucs), then mix stems, mute the part you want to play, loop sections, change speed and key, and keep practice notes. It runs on your own computer, needs no paid service, and works from your phone over Wi-Fi or Tailscale.

## Features
- **Split** any song into 4 stems (or 6 with guitar and piano), or just one part plus a backing track. Real progress bars for every step.
- **Practice player:** instant mute/solo, drag-to-scrub, A/B loop with named sections, speed 50-150% and key shift of up to 6 semitones (both without changing the other), a speed trainer that climbs each loop, a metronome with tap tempo.
- **Library** on your PC shared by every device: search, rename, delete (one or many), storage used, and "Practice again" that restores your last loop, speed, mix and notes.
- **Saved sessions** that live on the PC and sync between phone and computer.
- **Download the mix** (your levels, speed and key) as an MP3.
- Purple and silver theme with light and dark modes, installable on a phone's home screen.

## Setup
Requirements: Python 3.10-3.11, ffmpeg, and PyTorch (CPU works; an NVIDIA GPU with CUDA is much faster).

**Easy way (Windows):**
```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Cuda     # drop -Cuda for CPU-only
```
macOS/Linux: `./setup.sh` (or `./setup.sh cuda`). Both create a `venv`, install everything and create `passcode.txt`.

**By hand:**
```bash
python -m venv venv
venv\Scripts\activate          # Windows (use `source venv/bin/activate` on macOS/Linux)
# Install PyTorch first: pick CPU or CUDA at https://pytorch.org/get-started/locally/
pip install -r requirements.txt
```

## Run
Windows: double-click `start.bat`. Or in two terminals:
```bash
python -m uvicorn app:app --host 0.0.0.0 --port 8000
python web.py 5500
```
Open `http://localhost:5500/practice-cover-app-v7.html`.

### Passcode
If `passcode.txt` exists (or the `QT_PASSCODE` environment variable is set), the backend refuses every request without it, and the app asks for it once per device. Delete the file to turn the lock off. Restart the backend after changing it.

### From your phone
1. Find your PC's IPv4 address (`ipconfig`), or its Tailscale address (starts with `100.`) when away from home.
2. Open `http://<address>:5500/practice-cover-app-v7.html`. The backend address fills in automatically.
3. Share, then **Add to Home Screen** to install it. If it can't connect, allow Python through Windows Firewall for private networks only.

### HTTPS over Tailscale (recommended)
Plain `http` hides some browser features. Over HTTPS the phone gets the proper audio engine and a true full-screen app.
1. In the Tailscale admin console, turn on **MagicDNS** and **HTTPS Certificates**.
2. Run `powershell -ExecutionPolicy Bypass -File .\tailscale-https.ps1` on the PC (add `-Off` to undo). It publishes the page on port 443 and the backend on 8443, private to your tailnet.
3. Open the `https://<pc-name>.<tailnet>.ts.net/practice-cover-app-v7.html` address it prints, then add it to the home screen again.

### Start automatically at login (Windows)
`run-servers.ps1` keeps both servers running and restarts them if they stop. Register it once in PowerShell:
```powershell
Register-ScheduledTask -TaskName QuickTuneServers -User $env:USERNAME -RunLevel Limited -Action (New-ScheduledTaskAction -Execute powershell.exe -Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\path\to\QT-Stems\run-servers.ps1"') -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable)
```
Remove it with `Unregister-ScheduledTask QuickTuneServers`. Python is taken from `QT_PYTHON`, else the `demucs311` conda env, else `venv`, else PATH. The PC must be on, awake and logged in; the app shows a banner when it can't reach it.

## Models
- `htdemucs`: 4 stems (vocals, drums, bass, other)
- `htdemucs_6s`: adds guitar and piano (those two are the least clean)
- `htdemucs_ft`: same 4 stems, cleaner separation, about 4x slower
- `mdx_extra`: alternative 4-stem model (API only, not in the app)

"What to split" runs Demucs in two-stem mode: you get just the chosen part (vocals, drums, bass, guitar or piano) plus one backing track. Guitar and piano switch to the 6-stem model automatically. Demucs does not split beyond 6 stems. Pick GPU in the app only if CUDA-enabled PyTorch is installed (`python -c "import torch; print(torch.cuda.is_available())"`).

## How it works
- The app uploads a song, then polls `/api/jobs/<id>` for real separation progress (Demucs runs in a background thread, one job at a time).
- Stems are saved as 256 kbps MP3 under `stems/<id>/` with a `meta.json`; the original upload is kept in `library/`. Both are listed by the Library tab.
- Stems are downloaded fully to the device before the practice controls unlock. Playback uses Web Audio: every stem is decoded in memory and mixed inside one AudioWorklet on a single sample clock, so mute/solo/seek are instant and loops are sample-accurate. Speed and key use WSOLA time-stretching (key shift = stretch, then resample), so each can change without affecting the other. Very long songs are decoded at a lower sample rate to stay within phone memory.
- On plain `http` browsers hide AudioWorklet, so the same mixer runs on a `ScriptProcessorNode`. The line under "Stem mixer" shows which one is active.
- Saved sessions (`sessions.json`) live on the PC; the browser keeps a local copy as a fallback. The last song and per-song practice state are remembered in the browser.
- Downloading a mix renders the MP3 on the PC with ffmpeg.

## Tests
```bash
pip install -r requirements-dev.txt
pytest -q tests
```
The tests cover the passcode, upload limit, request validation, library, sessions and export; they never run Demucs. To run them on GitHub for every push, copy `ci/tests.yml` to `.github/workflows/tests.yml` (pushing a workflow file needs a token with the `workflow` scope, e.g. `gh auth refresh -s workflow`).

## Security note
This is a LAN/Tailscale tool. The optional passcode is a shared secret sent over your private network; over plain `http` anyone who can sniff that network could read it, so prefer the HTTPS setup when away from home. The page server (`web.py`) only serves the app files, never `passcode.txt`, `stems/` or `library/`, but do not expose ports 5500 or 8000 to the public internet. Uploads are capped at 500 MB (`QT_MAX_UPLOAD_MB`).

## Troubleshooting
- Failures are written to `last_error.txt` and shown at `/api/last-error`. `run-servers.log`, `backend_log.txt` and `web_log.txt` record the servers.
- "Can't reach your PC": the PC is asleep, off, or the servers have not started yet after a reboot. Open the Tailscale app, wake the PC, wait a few minutes after login.
- `CUDA error: unknown error` after a GPU driver update: restart the backend. Repeated blue screens mentioning `nvlddmkm.sys`: do a clean NVIDIA driver install and check overclocking tools.
- `favicon.ico` 404 and `GET /` 404 on the backend are harmless.

## License
MIT
