#!/bin/zsh
# install-hook.sh — register vibe-island-relay into Claude Code settings.
#
# Usage:
#   ./install-hook.sh             # install relay hooks
#   ./install-hook.sh --uninstall # remove only relay hooks
#   ./install-hook.sh --status    # show current state

set -euo pipefail

SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
RELAY_DIR="$HOME/Scripts/vibe-island"
RELAY="$RELAY_DIR/relay.py"
RELAY_CURRENT="$(cd "$(dirname "$0")" && pwd)/relay.py"
PRETOOL_MATCHER="Bash|Edit|Write|NotebookEdit|AskUserQuestion"
HOOK_EVENTS=(
  "SessionStart"
  "UserPromptSubmit"
  "PreToolUse"
  "PostToolUse"
  "PostToolUseFailure"
  "Stop"
  "StopFailure"
  "Notification"
  "PermissionRequest"
  "SessionEnd"
)

red()   { printf "\033[31m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
yellow(){ printf "\033[33m%s\033[0m\n" "$*"; }
bold()  { printf "\033[1m%s\033[0m\n" "$*"; }

ensure_settings() {
  mkdir -p "$(dirname "$SETTINGS")"
  [ -f "$SETTINGS" ] || printf '{\n  "hooks": {}\n}\n' > "$SETTINGS"
}

backup_once() {
  local ts="$(date +%Y%m%d-%H%M%S)"
  local bk="$SETTINGS.bak-pre-relay.$ts"
  if ! ls "$SETTINGS".bak-pre-relay.* >/dev/null 2>&1; then
    cp "$SETTINGS" "$bk"
    yellow "✓ backed up → $bk"
  fi
}

install_relay() {
  mkdir -p "$RELAY_DIR"
  cp "$RELAY_CURRENT" "$RELAY"
  chmod +x "$RELAY"
  green "✓ relay installed at $RELAY"
}

settings_edit() {
  local action="$1"
  python3 - "$SETTINGS" "$action" "$RELAY" "$PRETOOL_MATCHER" "${HOOK_EVENTS[@]}" <<'PYEOF'
import json
import sys

settings_path, action, relay, pretool_matcher = sys.argv[1:5]
events = sys.argv[5:]

with open(settings_path, "r", encoding="utf-8") as f:
    cfg = json.load(f)

hooks = cfg.setdefault("hooks", {})
relay_command = f"python3 {relay}"

def hook_commands(entry):
    if not isinstance(entry, dict):
        return []
    return [str(hook.get("command", "")) for hook in (entry.get("hooks") or []) if isinstance(hook, dict)]

def is_relay_entry(entry):
    for command in hook_commands(entry):
        if command == relay_command:
            return True
        normalized = command.lower().replace("\\", "/")
        if "vibe-island" in normalized and normalized.rstrip().endswith("/relay.py"):
            return True
    return False

def append_entry(entries, event):
    entry = {"hooks": [{"type": "command", "command": relay_command}]}
    if event == "PreToolUse":
        entry["matcher"] = pretool_matcher
    entries.append(entry)

for event in events:
    raw_entries = hooks.get(event, [])
    entries = raw_entries if isinstance(raw_entries, list) else []
    cleaned = [entry for entry in entries if not is_relay_entry(entry)]
    if action == "install":
        append_entry(cleaned, event)
    if cleaned:
        hooks[event] = cleaned
    else:
        hooks.pop(event, None)

if not hooks:
    cfg.pop("hooks", None)

with open(settings_path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")

if action == "install":
    print(f"installed relay hooks for {len(events)} event(s)")
else:
    print(f"removed relay hooks from {len(events)} event(s)")
PYEOF
}

show_status() {
  if [ ! -f "$SETTINGS" ]; then
    yellow "no settings.json at $SETTINGS"
    exit 0
  fi
  python3 - "$SETTINGS" "$RELAY" "${HOOK_EVENTS[@]}" <<'PYEOF'
import json
import sys

cfg = json.load(open(sys.argv[1], "r", encoding="utf-8"))
relay = sys.argv[2]
events = sys.argv[3:]
relay_command = f"python3 {relay}"

print("Event routing:")
for event in events:
    arr = cfg.get("hooks", {}).get(event, [])
    relay_count = 0
    other_count = 0
    for entry in arr if isinstance(arr, list) else []:
        commands = [str(h.get("command", "")) for h in (entry.get("hooks") or []) if isinstance(h, dict)]
        if relay_command in commands:
            relay_count += 1
        elif commands:
            other_count += 1
    tags = [f"relay={relay_count}", f"other={other_count}"]
    print(f"  {event:20s} {' '.join(tags)}")
PYEOF
}

case "${1:-install}" in
  --uninstall|-u)
    ensure_settings
    msg="$(settings_edit uninstall)"
    green "✓ $msg"
    ;;
  --status|-s)
    show_status
    ;;
  install|"")
    [ -f "$RELAY_CURRENT" ] || { red "✗ relay.py not found at $RELAY_CURRENT"; exit 1; }
    ensure_settings
    backup_once
    install_relay
    msg="$(settings_edit install)"
    green "✓ $msg"
    echo ""
    bold "Relay hooks now cover:"
    echo "  ${HOOK_EVENTS[*]}"
    echo "  PreToolUse matcher: $PRETOOL_MATCHER"
    echo "  PermissionRequest: blocking approval decisions"
    echo ""
    bold "Install properties:"
    echo "  - preserves non-relay hook entries"
    echo "  - removes only prior relay entries for idempotency"
    echo ""
    bold "Remove:  ./install-hook.sh --uninstall"
    ;;
  *)
    red "Unknown arg: $1"
    echo "Usage: $0 [--uninstall|--status]"
    exit 1
    ;;
esac
