# hermes-whatsapp-voice

Live voice calls with [Hermes](https://github.com/NousResearch/hermes-agent) over WhatsApp.

- **Call your bot.** Call your Hermes bot's WhatsApp number and talk to it. The voice agent can hand
  real work to Hermes mid-call ("move my 3pm to tomorrow", "what did Sam say about the invoice?").
- **Let Hermes call you.** For things that matter, Hermes can ring you instead of sending a message:
  a top-priority reminder, an urgent alert, a cron job that needs a decision. If you don't pick up,
  the tool says so and Hermes falls back to another channel.
- **Summary afterwards.** When a call ends, Hermes digests the transcript and sends you a short
  summary in WhatsApp.

It works with the personal-number setup most Hermes users already have (WhatsApp linked as a
device). No WhatsApp Business / Cloud API account is needed.

## How it works

```
 your phone ──WhatsApp call──▶ bot number ──▶ hermes-wa-voice (linked device, Rust)
                                                 │  call audio (16 kHz in, 24 kHz out, resampled)
                                                 ▼
                                            Gemini Live (speech-to-speech)
                                                 │  ask_hermes(...) tool calls
                                                 ▼
                                            Hermes API server (/v1/runs)
```

- **`hermes-wa-voice`** is a small sidecar built on
  [whatsapp-rust](https://github.com/oxidezap/whatsapp-rust). It links to the bot's WhatsApp number
  as one more linked device (WhatsApp allows four), answers calls from your number only, and places
  calls to your number only.
- Call audio is bridged to **Gemini Live** (`gemini-3.8-live` by default) for natural, interruptible
  speech-to-speech.
- The voice model gets two tools:
  - `ask_hermes`, which runs a Hermes turn through the API server and speaks the result when it
    arrives;
  - `end_call`, which hangs up after a goodbye.
- The **Hermes plugin** adds two agent tools: `whatsapp_call_owner(reason, details?)` and
  `whatsapp_call_status(call_id?)`. It also adds a `hermes whatsapp-voice` command for setup.

## Requirements

- Hermes `>= 0.21.5` on Linux or macOS.
- A separate WhatsApp number for the bot: Hermes's WhatsApp `bot` mode, or any spare number. In
  `self-chat` mode the bot is your own number, and you can't call yourself.
- Gemini set up in Hermes with an API key. The plugin reuses the key of Hermes's Gemini provider
  (`GOOGLE_API_KEY`, else `GEMINI_API_KEY`), so there is nothing extra to enter. Without one,
  `hermes model` (choose Gemini) adds it. Google login (Cloud Code OAuth) does not work for the
  Live API. The live models have a free tier, but Google may use free-tier data. Paid use is
  roughly $0.005/min of audio in and $0.018/min out.
- Optional, but needed for the voice agent to reach Hermes: the Hermes
  [API server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server) enabled.
  It must have an `API_SERVER_KEY`.

## Install

**Let your agent do it.** Tell Hermes (or any coding agent on that machine) to follow
[skills/setup/SKILL.md](skills/setup/SKILL.md). It's a step-by-step runbook written for agents,
and it stops to ask you only for your number and to scan a QR code. After installing, Hermes can
also load it as `hermes-whatsapp-voice:setup`.

**Or by hand:**

```bash
hermes plugins install ashwanthbalakrishnan5/hermes-whatsapp-voice --enable
```

### 1. Set your number

This is the only number that will ever be called, and the only caller that will be answered.

```bash
hermes config set plugins.entries.hermes-whatsapp-voice.settings.owner_number +15551234567
```

### 2. Link the sidecar to the bot's WhatsApp number (once)

Easiest: open the Hermes dashboard, go to the **WhatsApp Voice** tab, click **Pair with
WhatsApp**, and scan the QR from the **bot's** phone (WhatsApp > Linked devices > Link a device).
From a terminal instead:

```bash
hermes whatsapp-voice pair                        # shows a QR code
hermes whatsapp-voice pair --phone +15550001111   # the BOT's number; prints a pair code
```

The first run also fetches the sidecar binary for your platform from this repo's GitHub release.
You don't need Rust or any other toolchain. The download is checked against checksums pinned in
this repo (`sidecar/release-checksums.txt`). If the GitHub CLI is installed, its GitHub build attestation is checked too, which
proves it was built from this repository by its release workflow. Prebuilt binaries cover linux
amd64/arm64 and macOS arm64. To build it yourself instead (Rust 1.94+, cmake, a C compiler), run
`hermes whatsapp-voice install --from-source`.

> **Why a separate link when Hermes is already logged in to WhatsApp?** WhatsApp lets each linked
> device keep only one live connection. Sharing Hermes's WhatsApp session would make the two
> connections knock each other offline, and Hermes's messaging would break. Only the bot's phone
> can approve a new device, so the link happens once. It uses one of WhatsApp's four linked-device
> slots.

### 3. Enable the Hermes API server (recommended)

```bash
# in Hermes's .env (~/.hermes/.env by default)
API_SERVER_ENABLED=true
API_SERVER_KEY=<a long random string>
```

Restart the gateway after this.

### 4. Run the sidecar

```bash
hermes whatsapp-voice serve      # foreground, to try it out
hermes whatsapp-voice service    # prints a systemd user unit (Linux) or launchd plist (macOS)
```

To keep it running, save the printed unit and enable it as the comments in it describe. Check it
with:

```bash
hermes whatsapp-voice status
hermes whatsapp-voice call "Testing the new voice line"
```

## Using it

Call your bot's number from your phone. Or ask Hermes to call you:

> Call me at 7:30 tomorrow about the flight check-in, and if I don't answer, set an urgent reminder.

`whatsapp_call_owner` works from cron runs too, so a scheduled job can escalate to a phone call:

```bash
hermes cron create "0 9 * * 1-5" \
  "If anything on my calendar today is marked urgent, call me about it with whatsapp_call_owner."
```

The tool waits until the call is answered, declined or unanswered, and returns that status. On
`no_answer`/`rejected`/`failed` it tells the agent to use another channel.

## Configure

Everything is configurable from the **WhatsApp Voice** tab in the Hermes dashboard, the Desktop
app's plugin settings, or `hermes config set`. That covers the voice, the model, your own
instructions (language, tone, call length), and any Gemini Live option via `live_config`. The
voice takes its name and personality from your own Hermes. Full reference:
[docs/configuration.md](docs/configuration.md).

## What this plugin does that you should know about

- **Long-running process.** The sidecar runs as a separate process that you start (`serve`, or the
  systemd/launchd unit). The plugin itself only talks to it over loopback HTTP, authenticated with a
  random token stored in the plugin's data directory.
- **Stored credentials.** The sidecar's WhatsApp linked-device session is stored at
  `<hermes home>/plugin-data/hermes-whatsapp-voice/whatsapp.db` (the folder is private, mode 700).
  Anyone with that file can act as that linked device. Unlink it any time from the bot's phone
  (Linked devices).
- **Network calls.**
  - WhatsApp servers (signaling and call media relays).
  - `generativelanguage.googleapis.com` (Gemini), using the Gemini key Hermes already has. It
    receives call audio during calls, plus, as text: the persona briefing (Hermes's name,
    personality and what it knows about you), the reason and details of an outgoing call, and the
    results of `ask_hermes` requests.
  - Your local Hermes API server, using `API_SERVER_KEY`, for `ask_hermes` (which runs a full
    Hermes turn with your tools), the persona briefing (fetched at startup and after each call),
    and the post-call summary. That summary run asks Hermes to note anything worth remembering, so
    it can write to Hermes's memory.
  - `github.com`, to download the sidecar binary on first `pair`/`serve`/`install` (or from the
    dashboard's Pair button).

  Nothing else, and no telemetry.
- **Who it talks to.** It only answers calls from `owner_number` and only calls `owner_number`.
  Neither tool takes a destination parameter, so a prompt injection cannot make it call anyone else.
  Calls from other numbers are left alone, so the bot's other devices can still take them.
- **Commands it runs.**
  - The dashboard tab's backend (inside the Hermes dashboard, behind its login): `hermes config
    set` for this plugin's settings (only keys declared in `plugin.yaml`); the sidecar's `pair`;
    and `systemctl --user restart` or `launchctl kickstart` for the sidecar service.
  - The CLI: `gh attestation verify` when the GitHub CLI is logged in, and `cargo build` for
    `install --from-source`.
  - The setup skill (only when an agent follows it) appends `API_SERVER_ENABLED` and a generated
    `API_SERVER_KEY` to Hermes's `.env` if they're missing.
- **Secrets** are read through Hermes (`get_secret`) and passed to the sidecar in its environment.
  The plugin never reads `.env` itself. The sidecar's local API only binds to a loopback address.

## Caveats

- WhatsApp calling here uses an **unofficial**, reverse-engineered protocol (via whatsapp-rust).
  WhatsApp updates can break it, and automated use may violate WhatsApp's Terms of Service. Use a
  number you can afford to lose.
- One call at a time. Group calls and video are not supported.
- A Gemini Live connection lasts about 10 minutes. The sidecar resumes the session transparently, but
  a resume can fail and end a long call.

## Development

```bash
python -m pip install -r requirements-dev.txt
ruff check . && python -m pytest tests/ -q
cd sidecar && cargo test
GEMINI_API_KEY=... cargo test -- --ignored   # live round trip against Gemini
```

Layout: the repo root is the Hermes plugin (`plugin.yaml`, `__init__.py`, `tools.py`, `cli.py`,
`sidecar_client.py`), and `sidecar/` is the Rust sidecar. Tagging `vX.Y.Z` builds attested release
binaries (`.github/workflows/release.yml`). Then commit the release's `SHA256SUMS` as
`sidecar/release-checksums.txt`. Keep `version` in `plugin.yaml` and `sidecar/Cargo.toml` in step
with the tag.

## Credits

- [whatsapp-rust](https://github.com/oxidezap/whatsapp-rust) (MIT) does the WhatsApp protocol and the
  call media work.
- Gemini Live provides the voice.

## License

MIT


## Telegram Mini App (live voice, including Windows)

**New optional integration:** Talk to Hermes inside a Telegram Mini App using
Gemini 3.8 Live (microphone streaming over HTTPS/WSS). This does **not** replace or
modify the existing WhatsApp linked-device implementation.

- Windows PowerShell launcher: telegram_miniapp/start_windows.ps1
- Backend and Mini App: telegram_miniapp/server.py and telegram_miniapp/web/index.html
- Owner-only Telegram initData verification, Gemini Live audio relay, Hermes ask_hermes tool
- Installation, HTTPS proxy and configuration: [Telegram Mini App guide](docs/telegram-miniapp.md)
- GitHub Actions runs offline tests on Windows and Linux. Includes Gemini reconnect + short-lived session recovery, Gateway health alerts, animated waveform and speaker/mic controls.

**Important:** Telegram Mini App voice chat is not a native Telegram phone call.
The app requires a publicly reachable HTTPS URL and your own Telegram bot,
Gemini key and enabled Hermes API server. The original WhatsApp calls remain available.
\n### Windows gateway service, dashboard and audio QA\n\nThe optional Windows Service supervises the Mini App and only a gateway that it starts itself. Added readiness/Telegram alert retry with a disk-backed queue, Gemini reconnect preservation, separate microphone/Hermes waveforms, a dark/light voice dashboard, AudioWorklet 40ms PCM capture and a local microphone/speaker preflight. See [deployment and security guide](docs/telegram-miniapp.md) before installation. Hardware and live API E2E still require your own Windows/Telegram devices and secrets.\n
## R4: External Uptime, owner tool approval and release hardening

This optional Telegram integration now includes an independent external HTTPS uptime monitor, a fail-closed *per-tool* approval button in Telegram Mini App before Hermes can execute, a dry-run-first Windows Service release gate with source/config metadata snapshots and rollback, live RTT/audio buffer diagnostics, an adaptive playback queue and audio device hot-swap recovery. All existing WhatsApp features remain intact. See [full R4 setup and limitations](docs/telegram-miniapp.md).

### R5 (Draft)

Added Windows Service Release simulation in CI, Hermes risk approval and privacy-preserving audit, Redis-based external monitor coordination, owner-only health dashboards, accessibility controls, advanced audio input selection, network-aware buffering and opt-in short-lived session recovery. See [R5 setup and release limitations](docs/telegram-miniapp.md). Physical tests pending.

### R6: Failure injection, distributed Redis queue, secure approvals and adaptive Mini App

Adds fault-injection for Windows release rollback, SHA-256 validated backups, Redis leader-fenced alert queue, server-side tool permission ceiling and HMAC chained audit, hourly/daily readiness analytics, filterable Approval Center, WebView accessibility checks, smoother device switch and opt-in session retention. The original WhatsApp implementation is unchanged. See [deployment and remaining release blockers](docs/telegram-miniapp.md).
