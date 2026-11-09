# Starts GITS automatically when you log in to Windows, and keeps it running.
#
# MetaTrader 5 needs a logged-in desktop session, so the task runs at YOUR logon
# (not at boot). For a PC or VPS that must recover from reboots on its own,
# enable automatic sign-in for this Windows account as well.
#
# Run once in PowerShell from the "Gold trade" folder:
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1
# Remove with:
#   powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall_autostart.ps1

$ErrorActionPreference = "Stop"
$TaskName = "GITS Gold Bot"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Runner = Join-Path $PSScriptRoot "run_gits.bat"

if (-not (Test-Path (Join-Path $Root ".env"))) {
    Write-Warning "No .env found in $Root. Copy .env.example to .env and fill it in first."
}
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "python is not on PATH. Install Python 3.12+ and tick 'Add python.exe to PATH'."
}

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$Runner`"" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Installed scheduled task '$TaskName'. It starts at your next logon."
Write-Host "Start it now with:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Dashboard: http://localhost:8000   Logs: $Root\var\logs"
