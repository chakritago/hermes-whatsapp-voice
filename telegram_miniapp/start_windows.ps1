# Run from PowerShell after setting the required environment variables.
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $root
$required = @('TELEGRAM_BOT_TOKEN', 'TELEGRAM_OWNER_ID', 'API_SERVER_KEY', 'MINIAPP_PUBLIC_URL')
$missing = @($required | Where-Object { -not [Environment]::GetEnvironmentVariable($_) })
if (-not ($env:GOOGLE_API_KEY -or $env:GEMINI_API_KEY)) { $missing += 'GOOGLE_API_KEY or GEMINI_API_KEY' }
if ($missing.Count -gt 0) { throw ('Missing environment variables: ' + ($missing -join ', ')) }
if (-not $env:MINIAPP_PUBLIC_URL.StartsWith('https://')) { throw 'MINIAPP_PUBLIC_URL must be HTTPS' }
$python = (Get-Command py -ErrorAction SilentlyContinue)
if ($python) { & py -3 -m venv (Join-Path $root '.venv-telegram') }
else { & python -m venv (Join-Path $root '.venv-telegram') }
if ($LASTEXITCODE -ne 0) { throw 'Python 3 virtual environment creation failed' }
$venvPython = Join-Path $root '.venv-telegram\Scripts\python.exe'
& $venvPython -m pip install -r (Join-Path $root 'telegram_miniapp\requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
Write-Host 'Starting Hermes Telegram Mini App at http://127.0.0.1:8788'
& $venvPython -m telegram_miniapp.server
if ($LASTEXITCODE -ne 0) { throw 'Mini App server stopped with an error' }
