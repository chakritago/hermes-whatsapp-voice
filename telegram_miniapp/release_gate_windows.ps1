# Windows Service Release Gate — non-destructive by default.
# Runs on the Windows HOST (Administrator PowerShell). A dry-run is the default.
# Backup includes non-secret service metadata and versioned application source,
# not passwords, Telegram tokens, profile environment or binary dependencies.
[CmdletBinding()]
param(
    [ValidateSet('Preflight','Backup','Release','Rollback','Verify')]
    [string]$Mode = 'Preflight',
    [switch]$Apply,
    [string]$BackupPath = ''
)
$ErrorActionPreference = 'Stop'
$Name = 'HermesTelegramVoice'
if ($env:OS -ne 'Windows_NT') { throw 'This release gate requires Windows.' }
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$backupRoot = Join-Path $env:ProgramData 'HermesTelegramVoice\release-backups'

function Get-Svc {
    $s = Get-CimInstance Win32_Service -Filter "Name='$Name'" -ErrorAction SilentlyContinue
    if (-not $s) { throw "Service $Name is not installed. Install it and configure its restricted Log On account first." }
    if ($s.StartName -match '^(LocalSystem|NT AUTHORITY\\SYSTEM)$') {
        throw 'Refusing LocalSystem: configure a dedicated restricted service account first.'
    }
    return $s
}
function Test-Web {
    param([string]$Path)
    try {
        $r = Invoke-RestMethod -Uri ("http://127.0.0.1:8788/" + $Path) -TimeoutSec 7 -Method Get
        if ($Path -eq 'healthz') { return ($r.ok -eq $true) }
        return ($r.ready -eq $true)
    } catch { return $false }
}
function Test-Ready {
    for ($attempt = 1; $attempt -le 8; $attempt++) {
        $service = Get-Service -Name $Name -ErrorAction Stop
        if ($service.Status -eq 'Running' -and (Test-Web 'healthz') -and (Test-Web 'readyz')) { return $true }
        Start-Sleep -Seconds 5
    }
    return $false
}
function Save-Backup {
    $svc = Get-Svc
    [void](New-Item -ItemType Directory -Path $backupRoot -Force)
    $folder = Join-Path $backupRoot ((Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8))
    [void](New-Item -ItemType Directory -Path $folder -Force)
    $metadata = @{
        Version = 1
        Created = (Get-Date).ToString('o')
        Service = $Name
        WasRunning = ($svc.State -eq 'Running')
        StartMode = [string]$svc.StartMode
        StartAccount = [string]$svc.StartName
        # Service executable paths can include arguments; do not store credentials.
        SourceRoot = $root
        CodeFiles = @()
    }
    $source = Join-Path $root 'telegram_miniapp'
    $dest = Join-Path $folder 'telegram_miniapp'
    [void](New-Item -ItemType Directory -Path $dest -Force)
    foreach ($file in Get-ChildItem -LiteralPath $source -Recurse -File) {
        if ($file.Extension -notin @('.py','.ps1','.txt','.js','.html')) { continue }
        $relative = $file.FullName.Substring($root.Length).TrimStart('\')
        $target = Join-Path $folder $relative
        [void](New-Item -ItemType Directory -Path (Split-Path $target -Parent) -Force)
        Copy-Item -LiteralPath $file.FullName -Destination $target -Force
        $metadata.CodeFiles += $relative
    }
    $metadata | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $folder 'release.json') -Encoding UTF8
    # A service password, user env vars and API tokens are never in these backups.
    Write-Host ("Release snapshot: " + $folder)
    return $folder
}
function Restore-Backup {
    param([string]$Folder)
    $record = Join-Path $Folder 'release.json'
    if (-not (Test-Path -LiteralPath $record)) { throw 'Release backup metadata was not found.' }
    $old = Get-Content -LiteralPath $record -Raw | ConvertFrom-Json
    if ($old.Version -ne 1 -or $old.Service -ne $Name -or $old.SourceRoot -ne $root) {
        throw 'Backup schema, service or repository folder differs; refusing rollback.'
    }
    $svc = Get-Svc
    if ($svc.StartName -ne $old.StartAccount) {
        throw 'Service logon account changed. Reconcile account manually; password was not backed up.'
    }
    Write-Host "Rollback candidate: $Folder"
    Write-Host ('Files: ' + @($old.CodeFiles).Count + ', original mode: ' + $old.StartMode)
    if (-not $Apply) { Write-Host 'DRY RUN ONLY — pass -Apply for rollback.'; return }
    if ((Get-Service -Name $Name).Status -ne 'Stopped') {
        Stop-Service -Name $Name -Force -ErrorAction Stop
        (Get-Service -Name $Name).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
    }
    foreach ($relative in @($old.CodeFiles)) {
        if ($relative -notmatch '^telegram_miniapp[\\/]' -or $relative -match '\.\.') {
            throw 'Refusing unsafe path in backup'
        }
        $source = Join-Path $Folder $relative
        $target = Join-Path $root $relative
        if (-not (Test-Path -LiteralPath $source)) { throw "Missing backup file: $relative" }
        Copy-Item -LiteralPath $source -Destination $target -Force
    }
    $startup = switch ($old.StartMode) {
        'Auto' { 'Automatic' }
        'Manual' { 'Manual' }
        'Disabled' { 'Disabled' }
        default { throw "Unrecognized prior StartMode" }
    }
    Set-Service -Name $Name -StartupType $startup -ErrorAction Stop
    if ($old.WasRunning -and $startup -ne 'Disabled') {
        Start-Service -Name $Name -ErrorAction Stop
        if (-not (Test-Ready)) { throw 'Rollback restored code but readiness is still degraded. Investigate service logs.' }
    }
    Write-Host 'Rollback applied. Secrets, account password and system configuration were not changed.'
}
$svc = Get-Svc
Write-Host ("Service: " + $svc.Name + " | Account: " + $svc.StartName + " | State: " + $svc.State)
if ($Mode -eq 'Preflight') {
    if (-not (Test-Path -LiteralPath (Join-Path $root '.venv-telegram\Scripts\python.exe'))) { throw 'Windows Python venv missing.' }
    if (-not (Test-Path -LiteralPath (Join-Path $root 'telegram_miniapp\server.py'))) { throw 'Mini App not present.' }
    Write-Host ('Liveness: ' + (Test-Web 'healthz') + ' | Readiness: ' + (Test-Web 'readyz'))
    Write-Host 'Preflight completed. Secrets/account privileges, TLS/WSS and actual reboot still require operator verification.'
} elseif ($Mode -eq 'Verify') {
    if (-not (Test-Ready)) { throw 'Release gate failed: service is not ready.' }
    Write-Host 'Release gate health/readiness passed.'
} elseif ($Mode -eq 'Backup') {
    [void](Save-Backup)
} elseif ($Mode -eq 'Rollback') {
    if (-not $BackupPath) {
        $latest = Get-ChildItem -LiteralPath $backupRoot -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
        if (-not $latest) { throw 'No backup directory exists.' }
        $BackupPath = $latest.FullName
    }
    Restore-Backup -Folder $BackupPath
} elseif ($Mode -eq 'Release') {
    if (-not $Apply) { Write-Host 'DRY RUN — release requires -Mode Release -Apply'; return }
    $folder = Save-Backup
    try {
        Restart-Service -Name $Name -ErrorAction Stop
        if (-not (Test-Ready)) { throw 'Service failed readiness after restart' }
        Write-Host ('Release gate passed. Snapshot: ' + $folder)
    } catch {
        Write-Warning ('Release gate failed. Rolling back snapshot: ' + $_.Exception.Message)
        Restore-Backup -Folder $folder
        throw 'Release was rolled back after failed verification.'
    }
}
