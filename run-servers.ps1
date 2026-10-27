# Keeps the Quick Tune backend (port 8000) and web server (port 5500) running, restarting either if it exits.
# Used by the "QuickTuneServers" scheduled task so they start at login. Safe to run by hand too.
# Python: uses $env:QT_PYTHON, else the demucs311 conda env if present, else "python" from PATH.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = $env:QT_PYTHON
if (-not $py) {
    $conda = Join-Path $env:USERPROFILE 'miniconda3\envs\demucs311\python.exe'
    $py = if (Test-Path $conda) { $conda } else { 'python' }
}

Set-Content -Path (Join-Path $root 'backend_log.txt') -Value "run-servers started $(Get-Date)"

$loop = {
    param($py, $root, $port, $argList, $log)
    Set-Location $root
    while ($true) {
        if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
            Start-Sleep -Seconds 10   # something (e.g. start.bat) already serves this port
            continue
        }
        & $py @argList *>> $log
        Start-Sleep -Seconds 3
    }
}

$jobs = @(
    Start-Job -ScriptBlock $loop -ArgumentList $py, $root, 8000, @('-m', 'uvicorn', 'app:app', '--host', '0.0.0.0', '--port', '8000'), (Join-Path $root 'backend_log.txt')
    Start-Job -ScriptBlock $loop -ArgumentList $py, $root, 5500, @('-m', 'http.server', '5500', '--bind', '0.0.0.0'), (Join-Path $root 'web_log.txt')
)
Wait-Job $jobs | Out-Null
