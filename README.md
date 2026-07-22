# Vibe Island (Self-Made)

A self-made macOS Dynamic Island / notch panel for AI coding agents —
modeled after [vibeisland.app](https://vibeisland.app), built from scratch
in Swift/SwiftUI (~1500 lines).

Shows live status of your Claude Code / ZCode / Codex sessions in a notch
overlay at the top of your screen: what each agent is doing, Z.ai usage
quotas, and approval prompts for write operations.

## Features

- **Notch overlay** — top-center, dropdown-style shape (square top, rounded bottom)
- **Hover to expand** — NSTrackingArea, stable across size changes
- **Multi-agent session list** — scans running claude/zcode/codex processes
- **Real-time activity** — FileWatcher on rollout/transcript files (zero-delay)
- **Compact mode** — shows latest activity + pulse animation when agent is running
- **Usage monitoring** — Z.ai 5h/7d/monthly quotas with reset times + provider logo
- **Approval workflow** — PreToolUse hook for write tools (Bash/Edit/Write) → Allow/Deny
- **Jump to app** — click an agent card → activates its terminal (Warp/ZCode/iTerm)
- **DepartureMono font** — same font as the real app
- **Pulse bars** — animated equalizer when agent is actively working

## Quick Start

### Option A: Pre-built .app
```bash
./build-app.sh --install
open -a VibeIsland
```

### Option B: From source
```bash
./start.sh
```

### Enable Claude Code approval hooks
```bash
./install-hook.sh
```

This wires PreToolUse (write tools) → approval card in the notch.
To remove: `./install-hook.sh --uninstall`

### Enable usage monitoring
The usage daemon polls Z.ai quotas every 2 minutes:
```bash
python3 usage-daemon.py &
```
(Requires a Z.ai API key in `~/.zcode/v2/config.json`)

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  VibeIsland.app                  │
│  (SwiftUI notch overlay + TCP/Unix IPC servers)  │
└───────▲───────────────────▲──────────▲──────────┘
        │                   │          │
   TCP 14321          Unix Socket   File Watcher
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
| `usage-daemon.py` | Background Z.ai quota poller → TCP push |
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
