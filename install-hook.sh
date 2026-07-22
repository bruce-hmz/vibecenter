#!/bin/zsh
# install-hook.sh — register vibe-island-relay into Claude Code settings.
#
# Strategy: take over the 6 events that drive notch status display:
#   PreToolUse (approval on write tools)
#   SessionStart / UserPromptSubmit / PostToolUse / Stop / Notification
#     (non-blocking status updates so the notch shows "agent alive, doing X")
# The real app's bridge entries for these events are removed (they'd point
# at a non-running app and silently drop events). Other events the real
# app registered are left untouched.
#
# Usage:
#   ./install-hook.sh             # install (take over PreToolUse)
#   ./install-hook.sh --uninstall # restore real-app PreToolUse
#   ./install-hook.sh --status    # show current state

set -euo pipefail

SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
RELAY_DIR="$HOME/Scripts/vibe-island"
RELAY="$RELAY_DIR/relay.py"
RELAY_CURRENT="$(cd "$(dirname "$0")" && pwd)/relay.py"
MARKER="vibe-island-relay"
# Write tools that should trigger our approval card.
MATCHER="Bash|Edit|Write|NotebookEdit"
# Events we take over (status display + approval).
HOOK_EVENTS=("SessionStart" "UserPromptSubmit" "PreToolUse" "PostToolUse" "Stop" "Notification")

red()   { printf "\033[31m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
yellow(){ printf "\033[33m%s\033[0m\n" "$*"; }
bold()  { printf "\033[1m%s\033[0m\n" "$*"; }

ensure_settings() {
  mkdir -p "$(dirname "$SETTINGS")"
  [ -f "$SETTINGS" ] || printf '{\n  "hooks": {}\n}\n' > "$SETTINGS"
}

backup_once() {
  # Timestamped backup so we never clobber the user's earlier .bak from
  # the real app install. Only creates a fresh backup the first time.
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
  python3 - "$SETTINGS" "$action" "$RELAY" "$MATCHER" "${HOOK_EVENTS[@]}" <<'PYEOF'
import json, sys

settings_path, action, relay, matcher = sys.argv[1:5]
events = sys.argv[5:]

with open(settings_path, "r") as f:
    cfg = json.load(f)

hooks = cfg.setdefault("hooks", {})

def is_relay_entry(e):
    if not isinstance(e, dict):
        return False
    return any(relay in str(h.get("command", ""))
               for h in (e.get("hooks") or [{"command": ""}]))

def is_real_app_entry(e):
    if not isinstance(e, dict):
        return False
    return any("vibe-island-bridge" in str(h.get("command", ""))
               for h in (e.get("hooks") or [{"command": ""}]))

def cmd_for(event):
    """Build the relay command. PreToolUse gets a matcher; others don't."""
    return f"python3 {relay}"

removed_total = 0
touched = 0

for ev in events:
    arr = hooks.get(ev, [])
    if not isinstance(arr, list):
        arr = []
    before = len(arr)
    # Drop prior relay entries (idempotent) and real-app bridge entries
    # (so events don't route to a non-running app and silently drop).
    cleaned = [e for e in arr if not is_relay_entry(e) and not is_real_app_entry(e)]
    removed = before - len(cleaned)
    if action != "uninstall":
        # Append our entry. PreToolUse uses a matcher to only fire on write
        # tools; other events fire unconditionally.
        if ev == "PreToolUse":
            cleaned.append({"matcher": matcher,
                            "hooks": [{"type": "command", "command": f"python3 {relay}"}]})
        else:
            cleaned.append({"hooks": [{"type": "command", "command": f"python3 {relay}"}]})
    if cleaned:
        hooks[ev] = cleaned
    else:
        hooks.pop(ev, None)
    removed_total += removed
    touched += 1

if not hooks:
    cfg.pop("hooks", None)

with open(settings_path, "w") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")

if action == "uninstall":
    print(f"removed relay/real-app entries from {touched} event(s)")
else:
    print(f"took over {touched} event(s) (removed {removed_total} prior entries)")
PYEOF
}

show_status() {
  if [ ! -f "$SETTINGS" ]; then
    yellow "no settings.json at $SETTINGS"
    exit 0
  fi
  python3 - "$SETTINGS" "$RELAY" "${HOOK_EVENTS[@]}" <<'PYEOF'
import json, sys
cfg = json.load(open(sys.argv[1]))
relay = sys.argv[2]
events = sys.argv[3:]
print("Event routing:")
for ev in events:
    arr = cfg.get("hooks", {}).get(ev, [])
    ours = any(isinstance(e, dict) and any(relay in str(h.get("command",""))
               for h in (e.get("hooks") or [])) for e in arr)
    real = any(isinstance(e, dict) and any("vibe-island-bridge" in str(h.get("command",""))
              for h in (e.get("hooks") or [])) for e in arr)
    tag = []
    if ours: tag.append("✓ relay")
    if real: tag.append("⚠ real-app")
    if not tag: tag.append("— none")
    print(f"  {ev:20s} {' '.join(tag)}")
PYEOF
}

case "${1:-install}" in
  --uninstall|-u)
    ensure_settings
    settings_edit uninstall
    green "✓ $(settings_edit uninstall)"
    bold "Note: the real-app PreToolUse entry was removed on install."
    echo "      To restore it fully, copy from your .bak-pre-relay.* backup."
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
    bold "Notch status events now route to self-made app:"
    echo "  SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / Stop / Notification"
    echo "  PreToolUse matcher: $MATCHER (write tools → approval card)"
    echo ""
    bold "Test it:"
    echo "  1. Ensure notch app is running:  ./vibe-island-new"
    echo "  2. Open a Claude Code session → notch should show 'Session start'"
    echo "  3. Ask it to edit a file → approval card pops in the notch"
    echo ""
    bold "Remove:  ./install-hook.sh --uninstall"
    ;;
  *)
    red "Unknown arg: $1"
    echo "Usage: $0 [--uninstall|--status]"
    exit 1
    ;;
esac
