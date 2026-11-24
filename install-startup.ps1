# Run ONCE from an elevated PowerShell (right-click PowerShell > Run as administrator):
#     powershell -ExecutionPolicy Bypass -File .\install-startup.ps1
# Makes the backend and page server start when Windows boots, before anyone logs in, and keep running when you log out.
# It re-registers the "QuickTuneServers" task to run as your account with no stored password ("S4U"), so it has no
# network-drive access, which these servers don't need (D:\QuickTune is a local disk). Tailscale already starts at boot.
$ErrorActionPreference = 'Stop'
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Host 'Run this from an Administrator PowerShell.' -ForegroundColor Red; exit 1 }

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$me = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\run-servers.ps1`"" -WorkingDirectory $root
$triggers = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn -User $me))  # a second copy just waits (run-servers sees the ports taken)
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1)
Unregister-ScheduledTask -TaskName QuickTuneServers -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName QuickTuneServers -Action $action -Trigger $triggers -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName QuickTuneServers
Write-Host 'Done. The servers now start at boot, before login. Test it by restarting the PC and opening /status from another device.' -ForegroundColor Green
