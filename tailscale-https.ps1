# Publishes QT Stems over HTTPS on your tailnet using Tailscale Serve, so the phone gets a secure context
# (real AudioWorklet playback, true full-screen home-screen app) while away from home.
#
# One-time prerequisite, in the Tailscale admin console (https://login.tailscale.com/admin/dns):
#   turn on MagicDNS and "HTTPS Certificates".
#
# Usage:  powershell -ExecutionPolicy Bypass -File .\tailscale-https.ps1          (turn on)
#         powershell -ExecutionPolicy Bypass -File .\tailscale-https.ps1 -Off     (turn off)
param([switch]$Off)

$ts = (Get-Command tailscale -ErrorAction SilentlyContinue).Source
if (-not $ts) { $ts = 'C:\Program Files\Tailscale\tailscale.exe' }
if (-not (Test-Path $ts)) { Write-Error 'tailscale.exe not found. Install Tailscale first.'; exit 1 }

if ($Off) {
    & $ts serve --https=443 off
    & $ts serve --https=8443 off
    Write-Host 'Tailscale HTTPS publishing is off.'
    exit 0
}

# Web page on 443, backend API on 8443. Both stay private to your tailnet (this is "serve", not "funnel").
& $ts serve --bg --https=443 http://localhost:5500
& $ts serve --bg --https=8443 http://localhost:8000

$self = (& $ts status --json | ConvertFrom-Json).Self.DNSName.TrimEnd('.')
Write-Host ''
Write-Host "Open on your phone (Tailscale on):  https://$self/practice-cover-app-v7.html"
Write-Host 'Then use Share > Add to Home Screen again to replace the old shortcut.'
