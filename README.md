# Vibe Island (Self-Made)

A self-made macOS Dynamic Island / notch panel for AI coding agents —
modeled after [vibeisland.app](https://vibeisland.app), built from scratch
in Swift/SwiftUI.

Shows live status of your Claude Code / ZCode / Codex sessions in a notch
overlay at the top of your screen: what each agent is doing, Z.ai usage
quotas, and approval prompts for write operations.

## Features

- **Notch overlay** — top-center, dropdown-style shape (square top, rounded bottom)
- **Hover or click to expand** — preview on hover, click to pin the session center
- **Multi-agent session list** — scans running claude/zcode/codex processes
- **Real-time activity** — FileWatcher on rollout/transcript files (zero-delay)
- **Compact mode** — provider, current activity, pulse animation, and `+N` overflow
- **Usage monitoring** — Z.ai 5h/7d/monthly quotas with reset times + provider logo
- **Approval inbox** — queued write approvals and multi-question AskUserQuestion UI
- **Explainable approval risk** — low/medium/high/critical signals with human-readable reasons
- **Safe batch approval** — high and critical requests always require an individual decision
- **Private decision history** — stores only provider/tool category, risk, result, and time; never command, diff, path, prompt, or answer content
- **Fail-closed lifecycle** — visible countdown and automatic denial on timeout
- **Authenticated IPC** — HMAC-SHA256 signs every TCP request and response
- **Native notifications** — separate waiting, failure, and turn-complete preferences; notification bodies stay content-free
- **Multi-display placement** — automatic notch preference, main display, pointer-following, or a named display
- **Settings & health** — menu-bar entry, Hook repair, IPC/scanner/usage status
- **Return to work context** — PID app activation, terminal/provider fallback, then exact workspace fallback
- **DepartureMono font** — same font as the real app
- **Pulse bars** — animated equalizer when agent is actively working

## Quick Start

### Option A: Pre-built .app
```bash
./build-app.sh --install
open -a VibeIsland
```

After upgrading from an older build, open **Settings → Claude Code Hook** and
click **Install / Repair Hook** once so the relay uses authenticated IPC.

### Option B: From source
```bash
./start.sh
```

### Enable Claude Code approval hooks
```bash
./install-hook.sh
```

This wires the Claude Code lifecycle, permission, notification, successful-tool,
failed-tool, successful-stop, and failed-stop events into the island. PreToolUse
write tools still block on the approval card; attention events remain non-blocking.
The app bundle now ships with the hook installer/relay so the Settings UI can
install, repair, or remove the Hook directly. The script remains useful for
headless setup.
To remove: `./install-hook.sh --uninstall`

### Enable usage monitoring
The app bundle now includes `usage-daemon.py` so the app can manage quota
polling itself. For manual debugging, the daemon still works standalone:
```bash
python3 usage-daemon.py &
```
(Reads a Z.ai API key from `~/.zcode/v2/config.json`; poll interval/config path
can be overridden with `VIBE_ISLAND_USAGE_*` env vars for testing.)

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  VibeIsland.app                  │
│  (SwiftUI notch overlay + TCP/Unix IPC servers)  │
└───────▲───────────────────▲──────────▲──────────┘
        │                   │          │
 HMAC TCP 14321       Unix Socket   File Watcher
        │           ~/.vibe-island/   (rollout files)
        │           run/vibe-island.sock
        │                   │
  ┌─────┴─────┐      ┌──────┴──────┐
  │ relay.py  │      │ ZCode/Claude │
  │ (Claude   │      │ bridge events│
  │  hooks)   │      │ (NDJSON)     │
  └───────────┘      └─────────────┘
```

### Components

| File | Role |
|------|------|
| `VibeIsland.swift` | Core app: notch UI + state machine + IPC servers |
| `relay.py` | Claude Code hook → TCP bridge (PreToolUse approval) |
| `usage-daemon.py` | Background Z.ai quota poller → TCP push + lifecycle status |
| `scan-agents.sh` | Process scanner: discovers running agent sessions |
| `install-hook.sh` | Registers/removes Claude Code hook config |
| `build-app.sh` | Compiles + assembles .app bundle |

## Compatibility

- macOS 13+ (Ventura / Sonoma / Sequoia / Tahoe)
- Apple Silicon (arm64)
- Works with or without a physical notch (draws a fake one)

## Credits

- [Departure Mono](https://departuremono.com/) font by Helena Zhang (SIL OFL)
- Provider logos sourced from the real Vibe Island app bundle
- Inspired by [vibeisland.app](https://vibeisland.app)
