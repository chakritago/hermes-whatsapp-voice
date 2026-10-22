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

- Browser capture: microphone -> AudioWorklet -> mono signed PCM16 16 kHz in 40ms frames; ScriptProcessor is a fallback.
- Browser playback: signed PCM16 mono 24 kHz -> Web Audio AudioBuffer.
- WSS backend: aiohttp, Telegram HMAC validation, Gemini Live WebSocket, ask_hermes via Hermes API.
- Supports interruptions and live transcriptions; API secrets never appear in browser code.
- AudioWorklet runs audio conversion off the UI thread. Older Telegram WebViews fall back to ScriptProcessor.
- Telegram bot cannot initiate a native Telegram call. Existing WhatsApp calling
  remains available separately.
- Gemini Live automatically reconnects (up to four attempts, exponential backoff).
  Latest `sessionResumptionUpdate.newHandle` is used if Gemini marks it resumable.
  If Gemini has not supplied a valid handle, a new Gemini session is started and
  **voice context may be lost**. Hermes /v1/runs session ID remains unchanged.
- Browser WebSocket retries up to five times (1, 2, 4, 8, 16 seconds).
  An opaque, owner-bound recovery ticket lasts at most 120 seconds after disconnect,
  **only in server memory**; process restarts invalidate it. Signed Telegram
  initData is reverified on every connection; there is no anonymous bypass.
- A live status pill shows connecting/reconnecting/restored states. Waveform
  visualizer toggles between bars/line on tap, with reduced-motion support.
- Microphone mute disables the capture track and stops sending PCM frames.
  Output-device selection uses `HTMLMediaElement.setSinkId` when available.
  Telegram mobile WebViews often do not support speaker selection, so the
  device-default speaker remains the fallback.
- Hardware microphone and actual Gemini connections require manual end-to-end testing.

## Windows auto-start service and owned gateway supervision

The optional **real Windows Service** uses pywin32 and runs after reboot **without user login**.
It supervises the Mini App child and optionally an **owned** Hermes gateway child. It never
terminates or steals an already-running external Hermes instance.

1. Install Python 3.11+ in a machine-accessible folder and run the normal
   `telegram_miniapp/start_windows.ps1` once to create the venv; then stop it.
2. From an **elevated** PowerShell, run
   `./telegram_miniapp/install_windows_service.ps1`. This registers the service
   with startup set to **Automatic** but deliberately **does not start it yet**.
3. Create/select a **dedicated low-privilege account** with `Log on as a service`
   rights. In `services.msc` open **Hermes Telegram Voice → Properties → Log On**
   and set this account. Never run Hermes's arbitrary tools as LocalSystem.
4. Configure `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID`, `API_SERVER_KEY`,
   `GOOGLE_API_KEY` / `GEMINI_API_KEY`, and `MINIAPP_PUBLIC_URL` so they are
   available to that service account at boot. **Do not put tokens in Git, command
   lines, service display names or world-readable files.** The service identity
   must have read permissions for the repo, Python and venv; its writable
   profile must support the alert queue. Verify secrets are readable under
   that identity before enabling the service.
5. Set `HERMES_MINIAPP_PYTHON` to the absolute
   `<repo>\\.venv-telegram\\Scripts\\python.exe` path (under the service
   account), then run `Start-Service HermesTelegramVoice`. To inspect:
   `Get-Service HermesTelegramVoice`, `http://127.0.0.1:8788/healthz`
   and `http://127.0.0.1:8788/readyz`.
6. Optionally set `HERMES_GATEWAY_COMMAND` to an executable plus argv, preferably
   a JSON string array (e.g. `["hermes","gateway"]`) to supervise a **new gateway
   owned by this service**. Do not enable it if another Hermes gateway is managed
   by its own service/scheduled task. Use one owner per process.
7. Grant write permission on the profile-local alert queue and the
   `HERMES_SERVICE_LOG_DIR` location if configured, and configure Windows
   service failure actions if desired. Test a restart and full PC reboot.

The supervisor checks local Mini App liveness and authenticated Hermes
`/v1/capabilities` readiness. **Only its child processes** are restarted after
three failed probes (after a startup grace period), with bounded backoff.
The Windows service itself starts automatically after reboot; pywin32 may require
machine-wide install/postinstall on some Python distributions. Installation
must be validated on the target Windows host. This is *not* a guarantee of alert
delivery while the entire machine is offline.

## Dual Voice Waveforms, Preflight, and Themes

- Separate waveform visualizations for the microphone and actual Gemini playback,
  with textual `YOU` / `HERMES` speaking states and a pulsing Hermes avatar.
- An **Audio Device Preflight** button requests microphone permission, checks
  signal activity and plays a quiet 440Hz tone locally. No recording is uploaded.
- Three theme states: Auto (OS preference), Dark, Light. Theme preference
  is stored locally when Telegram WebView allows it.
- Reduced-motion setting removes animation; device-selector still falls back
  to system default if `setSinkId` is unsupported.

## Telegram Gateway Alerts

Enabled by default (disable with `GATEWAY_ALERTS_ENABLED=false`). The monitor:
- Probes Hermes `GET /health`, authenticated `GET /v1/capabilities` and Telegram `getMe` (read-only) every 60 seconds.
- Sends a Telegram `sendMessage` alert to **TELEGRAM_OWNER_ID only** after three
  consecutive readiness failures, and one recovery event.
- Failed deliveries are kept in a bounded **disk-backed FIFO** and retried with
  exponential backoff, including Telegram HTTP 429 `retry_after` hints.
  Queue persistence contains only generic down/recovered events — no secrets.
- Configure `GATEWAY_ALERT_QUEUE_FILE` for an explicit writable queue location
  if the default service-user profile is unavailable.
- Does **not** use `getUpdates`, `setWebhook`, or Telegram polling: existing bots unaffected.
- `GATEWAY_ALERT_INTERVAL` may be configured between 15 and 3600 seconds.
- Can detect API server failures **only while this Mini App backend is running**.
  If the entire Windows PC, local network or alert process fails, it cannot
  notify you. Use an independent uptime monitor for host-offline notifications.

The read-only readiness probes establish that the API server supports run
submission and Telegram bot credentials/network work. They do **not** confirm
the separate Telegram long-polling process is connected or every Hermes tool
works. `/readyz` on the Mini App returns **only** ready/degraded status, never
keys, Telegram account identifiers or internal URLs. `/healthz` remains a
simple Mini App liveness check.

## Validation

    python -m pip install -r telegram_miniapp/requirements.txt pytest ruff
    python -m pytest tests/test_telegram_miniapp.py tests/test_gateway_operations.py tests/test_voice_bridge_e2e.py -q
    node tests/test_audio_worklet.cjs
    ruff check telegram_miniapp tests/test_telegram_miniapp.py

GitHub Actions runs offline checks on Windows and Linux. For end-to-end testing, configure
your Telegram bot, HTTPS proxy, Hermes API and Gemini key, then launch from Telegram.


## Release R4 — Independent monitoring, approvals, diagnostics

**External Uptime Monitor:** install the Python monitor on a *different* continuously
running computer/VPS, not the Windows PC being watched. Install
\`pip install -r telegram_miniapp/requirements.txt\`, configure the monitor host's
\`HERMES_MONITOR_URL\` as your publicly reachable HTTPS origin,
\`TELEGRAM_BOT_TOKEN\` and \`TELEGRAM_OWNER_ID\` locally, then run
\`python -m telegram_miniapp.external_monitor\` under a persistent service
manager such as systemd. Optional \`HERMES_MONITOR_INTERVAL\` (default 60 seconds)
and \`EXTERNAL_MONITOR_QUEUE_FILE\` (path writable by monitor service).
The monitor reads **only** public \`/healthz\` and \`/readyz\`, refuses
non-HTTPS destinations and HTTP redirects, and sends a Telegram message after
three failures (and another when healthy). It cannot distinguish power loss from
an ISP outage, tunnel failure or other endpoint disruption. Both the monitor
and Telegram Bot API need network availability; alert retries are persisted.
Do not store bot credentials in source control.

**Hermes Tool Approval:** every Gemini \`ask_hermes\` tool call, including reads,
is blocked until the same **owner-authenticated Telegram Mini App WebSocket**
receives a specific **Approve this time** click. The exact requested text is
displayed in the Mini App; automatic denial occurs after 45 seconds, on
disconnect, on cancellation, on explicit Deny or if the app closes. Approval
tokens are single-use and expire; voice/LLM statements cannot grant permission.
No Telegram webhook or callback-query handler is taken over. This first
release is intentionally fail-closed for *all* Hermes calls, as a heuristic
risk classifier might misclassify a dangerous operation. Do not confuse
Gemini reading a spoken "yes" with explicit button approval.

**Windows Service Release Gate:** run from elevated PowerShell in repo root:
- \`.\telegram_miniapp\release_gate_windows.ps1 -Mode Preflight\`: checks
  service identity, local venv and endpoint liveness/readiness; doesn't mutate.
- \`.\telegram_miniapp\release_gate_windows.ps1 -Mode Backup\`: saves
  non-secret service identity, start mode, running state plus a snapshot of
  source files (\`.py\`, \`.js\`, \`.html\`, \`.ps1\`, \`.txt\`) under ProgramData.
  Take the backup **before** replacing deployed code.
- \`.\telegram_miniapp\release_gate_windows.ps1 -Mode Release\`: dry-run by default.
  Add \`-Apply\` to back up current files, restart the service and verify
  \`/healthz\` + \`/readyz\`, with automatic rollback on failure.
- \`.\telegram_miniapp\release_gate_windows.ps1 -Mode Rollback\`: dry-run by default,
  showing the latest snapshot; add \`-Apply\` to restore code and service
  startup/running state. Use \`-BackupPath\` to choose an older snapshot.

**Rollback boundaries:** It cannot restore passwords, API keys, environment
variables, Windows service logon passwords, operating-system changes or untracked
user data. It restores only the explicitly listed source files, not Python
dependency versions or files newly introduced after the backup. Run it as
Administrator after protecting the ProgramData backup directory with appropriate
NTFS ACLs. Never rely on this as the only system backup.

**Voice Diagnostics and Recovery:** the dashboard shows RTT from authenticated
WebSocket ping/pong, adaptive queued audio buffer duration, microphone activity,
network quality bucket, and counters for audio drops/underruns. The Gemini
playback scheduling target adapts to observed RTT variability and caps excessive
backlog. This is a best-effort jitter buffer, not a guaranteed latency SLA.
Smart device recovery listens for \`devicechange\`/ended audio tracks and attempts
a new mic capture (with browser permission) and default speaker fallback;
Telegram WebViews may restrict device enumeration or automatic recovery.
Avatar reflects idle/listening/speaking/approval/reconnecting states with
reduced-motion support.

### R4 verification / live blockers

Offline CI covers Windows and Linux Python security/monitor tests, mocked Gemini
WebSocket tool execution with owner approval, HTTP read-only monitor, AudioWorklet
PCM, adaptive buffer, and dashboard boot/DOM sanity. **Still required before
production:** physical Windows service install/reboot and credential access under
restricted account; actual second-machine outage simulation; live Telegram
Mini App approval on mobile/desktop; real Gemini audio latency and device hot-swap.
