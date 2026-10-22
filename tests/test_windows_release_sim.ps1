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
function Stop-Service { param($Name,[switch]$Force,$ErrorAction)
    if ($global:failStop) { throw 'Injected Stop-Service failure' }
    $global:fakeServiceStatus='Stopped'
}
function Start-Service { param($Name,$ErrorAction) $global:fakeServiceStatus='Running' }
function Restart-Service { param($Name,$ErrorAction)
    if ($global:failRestart) { throw 'Injected Restart-Service failure' }
    $global:fakeRestarts++;$global:fakeServiceStatus='Running'
}
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
if ($global:fakeStartMode -notin @('Auto','Automatic')) { throw 'Expected Automatic startup mode before simulated reboot' }
Start-Service -Name 'HermesTelegramVoice'
& $scriptPath -Mode Verify
if ($global:fakeServiceStatus -ne 'Running') { throw 'Automatic service startup simulation failed' }
# Fault injection: corrupt backup must never be copied to a running installation.
$liveFile = Join-Path $scriptDirectory 'server.py'
$liveBefore = Get-Content -LiteralPath $liveFile -Raw
[IO.File]::WriteAllText((Join-Path $backupDir.FullName 'telegram_miniapp\server.py'), 'tampered')
$rejected = $false
try { & $scriptPath -Mode Rollback -BackupPath $backupDir.FullName -Apply }
catch { $rejected = ($_.Exception.Message -match 'Corrupted backup') }
if (-not $rejected) { throw 'Corrupted backup was accepted' }
if ((Get-Content -LiteralPath $liveFile -Raw) -ne $liveBefore) { throw 'Corrupted backup mutated live code' }
Write-Host 'PASS: invalid backup hash rejected without modifying live code'

# Fault injection: Service fails to restart, the automatic restore must recover.
$global:failRestart = $true
$rollbackDetected = $false
try { & $scriptPath -Mode Release -Apply }
catch { $rollbackDetected = ($_.Exception.Message -match 'snapshot restored') }
$global:failRestart = $false
if (-not $rollbackDetected) { throw 'Failed Service restart was not rolled back' }
if ($global:fakeServiceStatus -ne 'Running') { throw 'Service not restored after injected restart failure' }
Write-Host 'PASS: injected Service restart failure triggers automatic rollback'

# Fault injection: rollback itself fails, must surface a CRITICAL error and snapshot.
$global:failRestart = $true
$global:failStop = $true
$criticalDetected = $false
try { & $scriptPath -Mode Release -Apply }
catch { $criticalDetected = ($_.Exception.Message -match 'CRITICAL: release failed AND automatic rollback failed') }
$global:failRestart = $false
$global:failStop = $false
if (-not $criticalDetected) { throw 'Double failure did not report CRITICAL recovery steps' }
Write-Host 'PASS: injected rollback failure reports CRITICAL and snapshot location'

Write-Host 'PASS: simulated auto-start after boot transition (no OS reboot)'
Write-Host 'PASS: simulated Windows preflight/backup/dry-run/restart/rollback/verification'
Write-Host 'NO physical Windows Service install or reboot was performed.'
