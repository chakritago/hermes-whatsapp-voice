# Telegram Mini App — Live voice with Hermes (Windows / Linux)

This is **additive**: the existing WhatsApp Rust sidecar, tools and installation remain unchanged.
Telegram Bot API does not provide ordinary private voice calls. This is an HTTPS Mini App
opened from a Telegram bot chat, with live microphone audio streamed over WSS to your server.
The server talks to **Gemini 3.8 Live**, then uses Hermes /v1/runs when Gemini calls ask_hermes.

## Prerequisites

- Python 3.11+ on Windows 10/11 or Linux
- Existing Hermes installation with API_SERVER_ENABLED=true
- API_SERVER_KEY used by that server; Gemini API key with Gemini Live access
- Telegram bot token from @BotFather and **your numeric Telegram user ID**
- Public **HTTPS** URL (separate domain/subdomain) reverse-proxied to **127.0.0.1:8788**.
  HTTPS must support WebSocket upgrade for /ws (WSS). Use TLS reverse proxy or a secure tunnel;
  do not open port 8788 directly to the internet.

## Windows quick start (PowerShell)

Set these environment variables in the **same terminal**, substituting your real values:

- TELEGRAM_BOT_TOKEN: Telegram bot token from BotFather
- TELEGRAM_OWNER_ID: your numeric Telegram user ID (not @username)
- GOOGLE_API_KEY (or GEMINI_API_KEY): Gemini Live key
- API_SERVER_KEY: the same secret as the running Hermes API server
- MINIAPP_PUBLIC_URL: e.g. https://voice.example.org (your actual HTTPS endpoint)
- HERMES_API_URL (optional): defaults to http://127.0.0.1:8642
- MINIAPP_PORT (optional): defaults to 8788

Never commit secret values. Your existing Hermes Telegram bot may share the same token.
This integration **does not call setWebhook, deleteWebhook or getUpdates**, so it
will not take over an existing Hermes Telegram polling/webhook channel.

From the repository root:

    .\telegram_miniapp\start_windows.ps1

In a second terminal using the same environment variables:

    .\.venv-telegram\Scripts\python.exe -m telegram_miniapp.configure_bot

This sets the **menu button only for TELEGRAM_OWNER_ID's chat** to the Mini App URL.
Open your bot's Telegram chat, tap **Talk to Hermes**, and grant microphone permission.

## Linux

    python3 -m venv .venv-telegram
    .venv-telegram/bin/pip install -r telegram_miniapp/requirements.txt
    .venv-telegram/bin/python -m telegram_miniapp.configure_bot
    .venv-telegram/bin/python -m telegram_miniapp.server

Run via your existing service manager; protect secrets as environment variables.

## Reverse proxy / security

- Server binds to **127.0.0.1** only. HTTPS/WSS termination at the proxy is required.
- MINIAPP_PUBLIC_URL is used to verify Origin; URL must match what Telegram launches.
- Telegram initData HMAC is validated server-side, checked for <=24h age, and compared
  with TELEGRAM_OWNER_ID. No anonymous browser test bypass is enabled.
- Bot token, Hermes key and Gemini key must stay on the Windows host / server.
- Maximum one live conversation at a time; each session terminates at 15 minutes.
- Voice audio goes to Google's Gemini Live API. Hermes requests may perform actions
  with the permissions your Hermes agent already possesses.
- Audio is not recorded by this bridge; the UI shows ephemeral transcriptions.

## Implementation / limits

- Browser capture: microphone -> Web Audio ScriptProcessor -> mono signed PCM16 16 kHz.
- Browser playback: signed PCM16 mono 24 kHz -> Web Audio AudioBuffer.
- WSS backend: aiohttp, Telegram HMAC validation, Gemini Live WebSocket, ask_hermes via Hermes API.
- Supports interruptions and live transcriptions; API secrets never appear in browser code.
- ScriptProcessor is a compatibility first release. AudioWorklet is recommended for
  lower-jitter production audio on supported Telegram WebViews.
- Telegram bot cannot initiate a native Telegram call. Existing WhatsApp calling
  remains available separately.
- Gemini may end or rotate a Live session; the user can reconnect. Automatic session
  resumption is not implemented in this initial release.
- Hardware microphone and actual Gemini connections require manual end-to-end testing.

## Validation

    python -m pip install -r telegram_miniapp/requirements.txt pytest ruff
    python -m pytest tests/test_telegram_miniapp.py -q
    ruff check telegram_miniapp tests/test_telegram_miniapp.py

GitHub Actions runs offline checks on Windows and Linux. For end-to-end testing, configure
your Telegram bot, HTTPS proxy, Hermes API and Gemini key, then launch from Telegram.
