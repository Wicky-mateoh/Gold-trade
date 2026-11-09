# Removes the GITS auto-start task. Does not delete any data in var\.
$TaskName = "GITS Gold Bot"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
} else {
    Write-Host "No task named '$TaskName' is installed."
}
Write-Host "If a GITS window is still running, close it (or end the python.exe running uvicorn)."
