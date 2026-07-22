#!/bin/zsh
# start.sh — launch the notch app + usage daemon together.
# Use this if running from source (not the .app bundle).

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

# Kill any existing instances.
pkill -f "vibe-island-new" 2>/dev/null || true
pkill -f "usage-daemon.py" 2>/dev/null || true
sleep 0.5

# Start the notch app.
if [ -f "$DIR/vibe-island-new" ]; then
  nohup "$DIR/vibe-island-new" > /tmp/vibe_island.log 2>&1 &
  echo "✓ notch app started (PID $!)"
else
  echo "✗ vibe-island-new not found — run ./build-app.sh first"
  exit 1
fi

# Start the usage daemon (Z.ai quota polling).
if [ -f "$DIR/usage-daemon.py" ]; then
  nohup python3 "$DIR/usage-daemon.py" > /tmp/vibe-usage.log 2>&1 &
  echo "✓ usage daemon started (PID $!)"
fi

echo ""
echo "Notch running. Ctrl+C to stop (or pkill -f vibe-island-new)"
