$ErrorActionPreference = 'Stop'
$pilotTaskName = 'LifeAssistantPilot-Startup'
$pilotScript = Join-Path $PSScriptRoot 'Pilot-Watchdog.ps1'
if (-not (Test-Path -LiteralPath $pilotScript)) { throw 'Watchdog missing' }
if (Get-ScheduledTask -TaskName $pilotTaskName -ErrorAction SilentlyContinue) { throw 'Pilot startup task already exists; review it before updating' }
$pilotAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -WindowStyle Hidden -File "' + $pilotScript + '"')
$pilotTrigger = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
$pilotSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $pilotTaskName -Action $pilotAction -Trigger $pilotTrigger -Settings $pilotSettings -Description 'Keep only the isolated LifeAssistantPilot environment available after login' | Out-Null
Write-Output 'Pilot login startup task installed. Reboot verification still required.'
