# Install the REAL Windows Service; does not modify existing Hermes services.
# Before running: set service-account secrets and Hermes/Gemini/Telegram
# environment under a dedicated, least-privileged Windows service account.
# Run this installer in an elevated PowerShell.
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -and $env:OS -ne 'Windows_NT') { throw 'Windows only' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run PowerShell as Administrator'
}
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$py = Join-Path $root '.venv-telegram\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) { throw 'Run telegram_miniapp\start_windows.ps1 to install the venv first' }
& $py -m pip install "pywin32>=306,<400"
if ($LASTEXITCODE -ne 0) { throw 'pywin32 installation failed' }
# Service must import this repo even when started with C:\Windows\System32 as CWD.
$sites = & $py -c "import site;print(site.getsitepackages()[0])"
if ($LASTEXITCODE -ne 0) { throw 'Unable to locate venv site-packages' }
[IO.File]::WriteAllText((Join-Path $sites 'hermes_telegram_voice.pth'), $root + [Environment]::NewLine)
# This registers the service but deliberately does NOT start it as LocalSystem.
# Configure Log On in services.msc with a dedicated restricted account,
# then set HERMES_MINIAPP_PYTHON to the absolute venv Python and ensure
# API keys are available to THAT account before starting.
& $py -m telegram_miniapp.windows_service install --startup auto
if ($LASTEXITCODE -ne 0) { throw 'Windows service registration failed' }
Write-Host 'Service installed but NOT started. In services.msc set Hermes Telegram Voice Log On to a dedicated service account.'
Write-Host 'Configure HERMES_MINIAPP_PYTHON and credentials under that account; then start the service.'
Write-Host 'After configuration: Start-Service HermesTelegramVoice'
