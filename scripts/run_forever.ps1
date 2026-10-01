<#
Keeps Plough Backer running across hard process crashes (README §73).

  Install once, as the Windows user that runs MT5:
    powershell -ExecutionPolicy Bypass -File scripts\run_forever.ps1 -Install [-Python C:\path\to\venv\Scripts\python.exe]
  Run by hand:
    powershell -ExecutionPolicy Bypass -File scripts\run_forever.ps1

The task starts at logon (MT5 needs a desktop session, so not a service). Task Scheduler
restarts this wrapper if it dies; the wrapper restarts Python whenever it exits.
Exit code 2 (startup refused: config/migrations) stops the loop, since a restart can't fix it.
#>
param([switch]$Install, [string]$Python = "python")

$root = Split-Path $PSScriptRoot -Parent
$python = (Get-Command $Python -ErrorAction Stop).Source  # absolute: the task has no PATH guarantees

if ($Install) {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $root `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" -Python `"$python`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName "PloughBacker" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Output "Installed task 'PloughBacker'. Start now: Start-ScheduledTask PloughBacker"
    exit 0
}

Set-Location $root
# ponytail: log grows unbounded; rotate (or add a RotatingFileHandler) if it gets large.
$log = Join-Path $root "data\plough_backer.log"
$delay = 5
while ($true) {
    $started = Get-Date
    # cmd does the redirect: PowerShell 5.1 would wrap each native stderr line in an ErrorRecord.
    cmd /c "`"$python`" -m plough_backer.main >> `"$log`" 2>&1"
    $code = $LASTEXITCODE
    if ($code -eq 2) {
        Add-Content $log "$(Get-Date -Format o) run_forever: startup refused (exit 2), not restarting"
        exit 2
    }
    if (((Get-Date) - $started).TotalSeconds -ge 600) { $delay = 5 }  # healthy run resets backoff
    Add-Content $log "$(Get-Date -Format o) run_forever: exited with $code, restarting in ${delay}s"
    Start-Sleep -Seconds $delay
    $delay = [Math]::Min($delay * 2, 300)
}
