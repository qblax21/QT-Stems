# One-time setup on Windows: creates a virtual environment, installs PyTorch + the requirements,
# and creates a passcode. Run from the project folder:
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1          (CPU PyTorch)
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Cuda    (NVIDIA GPU, much faster)
param([switch]$Cuda)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

$py = $null
foreach ($v in '3.11', '3.10') {
    if (Get-Command py -ErrorAction SilentlyContinue) { & py "-$v" --version 2>$null | Out-Null; if ($LASTEXITCODE -eq 0) { $py = @('py', "-$v"); break } }
}
if (-not $py) {
    if (Get-Command python -ErrorAction SilentlyContinue) { $py = @('python') }
    else { throw 'Python 3.10 or 3.11 was not found. Install it from python.org, then run this again.' }
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Warning 'ffmpeg is not on PATH. Install it (winget install ffmpeg) before splitting songs.'
}

if (-not (Test-Path venv)) { & $py[0] $py[1..($py.Count)] -m venv venv }
$vpy = Join-Path $PWD 'venv\Scripts\python.exe'
& $vpy -m pip install --upgrade pip
if ($Cuda) { & $vpy -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu118 }
else { & $vpy -m pip install torch torchaudio }
& $vpy -m pip install -r requirements.txt

if (-not (Test-Path passcode.txt)) {
    $alphabet = 'abcdefghjkmnpqrstuvwxyz23456789'.ToCharArray()
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $groups = 1..3 | ForEach-Object { -join (1..4 | ForEach-Object { $b = New-Object byte[] 1; do { $rng.GetBytes($b) } while ($b[0] -ge 248); $alphabet[$b[0] % 31] }) }
    ($groups -join '-') | Set-Content passcode.txt
    Write-Host "Created passcode.txt (the app asks for it): $($groups -join '-')"
}
Write-Host ''
Write-Host 'Done. Start the servers with start.bat, then open http://localhost:5500/practice-cover-app-v7.html'
