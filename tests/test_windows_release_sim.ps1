# CI-only isolated simulation: no real Windows service is installed/started/restarted.
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$fixture = Join-Path ([IO.Path]::GetTempPath()) ("hermes-gate-test-" + [Guid]::NewGuid().ToString('N'))
$env:ProgramData = Join-Path $fixture 'ProgramData'
$scriptDirectory = Join-Path $fixture 'telegram_miniapp'
[void](New-Item -ItemType Directory -Force -Path $scriptDirectory)
[void](New-Item -ItemType Directory -Force -Path (Join-Path $fixture '.venv-telegram\Scripts'))
[IO.File]::WriteAllText((Join-Path $fixture '.venv-telegram\Scripts\python.exe'), 'dummy')
Copy-Item -LiteralPath (Join-Path $repo 'telegram_miniapp\release_gate_windows.ps1') -Destination (Join-Path $scriptDirectory 'release_gate_windows.ps1')
[IO.File]::WriteAllText((Join-Path $scriptDirectory 'server.py'), 'v1')
[IO.File]::WriteAllText((Join-Path $scriptDirectory 'component.js'), 'v1')
$env:OS = 'Windows_NT'
$global:fakeServiceStatus = 'Running'
$global:fakeStartMode = 'Auto'
$global:fakeRestarts = 0
function Get-CimInstance { param($ClassName,$Filter) return [pscustomobject]@{
    Name='HermesTelegramVoice';StartName='.\RestrictedHermes';State=$global:fakeServiceStatus;StartMode=$global:fakeStartMode
} }
function Get-Service {
    param([string]$Name)
    $obj = [pscustomobject]@{Status=$global:fakeServiceStatus}
    $obj | Add-Member -MemberType ScriptMethod -Name WaitForStatus -Value {param($s,$t)} -Force
    return $obj
}
function Invoke-RestMethod { param($Uri,$TimeoutSec,$Method) return @{
    ok=$true;ready=$true
} }
function Start-Sleep { param($Seconds) }
function Stop-Service { param($Name,[switch]$Force,$ErrorAction) $global:fakeServiceStatus='Stopped' }
function Start-Service { param($Name,$ErrorAction) $global:fakeServiceStatus='Running' }
function Restart-Service { param($Name,$ErrorAction) $global:fakeRestarts++;$global:fakeServiceStatus='Running' }
function Set-Service { param($Name,$StartupType,$ErrorAction) $global:fakeStartMode=$StartupType }
$scriptPath = Join-Path $scriptDirectory 'release_gate_windows.ps1'
& $scriptPath -Mode Preflight
& $scriptPath -Mode Backup
$backupDir = Get-ChildItem -LiteralPath (Join-Path $env:ProgramData 'HermesTelegramVoice\release-backups') -Directory | Select-Object -First 1
if (-not $backupDir) { throw 'Backup snapshot missing' }
[IO.File]::WriteAllText((Join-Path $scriptDirectory 'server.py'), 'v2')
& $scriptPath -Mode Release
if ($global:fakeRestarts -ne 0) { throw 'Dry-run mutated service' }
& $scriptPath -Mode Release -Apply
if ($global:fakeRestarts -ne 1) { throw 'Release failed to restart the mocked service' }
& $scriptPath -Mode Rollback -BackupPath $backupDir.FullName
if ((Get-Content (Join-Path $scriptDirectory 'server.py') -Raw).Trim() -ne 'v2') {
    throw 'Dry-run rollback mutated source'
}
& $scriptPath -Mode Rollback -BackupPath $backupDir.FullName -Apply
if ((Get-Content (Join-Path $scriptDirectory 'server.py') -Raw).Trim() -ne 'v1') {
    throw 'Rollback did not restore snapshot'
}
& $scriptPath -Mode Verify
$global:fakeServiceStatus='Stopped'
& $scriptPath -Mode Preflight
# Simulate the service controller starting an Auto-mode service at boot.
# This is a state-machine simulation only, not an operating-system reboot.
if ($global:fakeStartMode -ne 'Auto') { throw 'Expected Automatic startup mode before simulated reboot' }
Start-Service -Name 'HermesTelegramVoice'
& $scriptPath -Mode Verify
if ($global:fakeServiceStatus -ne 'Running') { throw 'Automatic service startup simulation failed' }
Write-Host 'PASS: simulated auto-start after boot transition (no OS reboot)'
Write-Host 'PASS: simulated Windows preflight/backup/dry-run/restart/rollback/verification'
Write-Host 'NO physical Windows Service install or reboot was performed.'
