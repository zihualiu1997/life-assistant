param([string]$Distro = 'LifeAssistantPilot')
$ErrorActionPreference = 'Stop'
if ($Distro -ne 'LifeAssistantPilot') { throw 'Only the isolated pilot distribution is allowed' }
while ($true) {
    & wsl.exe -d $Distro --exec /bin/bash -lc 'if test -f /etc/systemd/system/life-pilot.service && ! test -f /var/lib/life-pilot/PAUSED; then systemctl start life-pilot.service; fi; exec sleep infinity'
    Start-Sleep -Seconds 15
}
