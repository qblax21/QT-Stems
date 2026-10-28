@echo off
rem Starts the Quick Tune backend (port 8000) and the web page server (port 5500).
rem Uses the demucs311 conda env if present, else venv\Scripts\python.exe (setup.ps1), else "python" from PATH. To use a specific environment, run this from an activated
rem prompt (e.g. "conda activate demucs311") or set QT_PYTHON to the full path of python.exe.
cd /d "%~dp0"
if "%QT_PYTHON%"=="" if exist "%USERPROFILE%\miniconda3\envs\demucs311\python.exe" set QT_PYTHON=%USERPROFILE%\miniconda3\envs\demucs311\python.exe
if "%QT_PYTHON%"=="" if exist "%~dp0venv\Scripts\python.exe" set QT_PYTHON=%~dp0venv\Scripts\python.exe
if "%QT_PYTHON%"=="" set QT_PYTHON=python
start "Quick Tune backend (8000)" "%QT_PYTHON%" -m uvicorn app:app --host 0.0.0.0 --port 8000
start "Quick Tune web (5500)" "%QT_PYTHON%" web.py 5500
echo.
echo Open on this PC:   http://localhost:5500/practice-cover-app-v7.html
echo Your LAN address (use on phone, same Wi-Fi):
ipconfig | findstr /R /C:"IPv4"
echo Tailscale address starts with 100. - use that when away from home.
