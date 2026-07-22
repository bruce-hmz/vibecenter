#!/bin/zsh
# scan-agents.sh — discover currently-running agent CLIs and emit one NDJSON
# line per session, suitable for piping into the notch app's TCP IPC.
#
# For each session we try to surface a richer task description by reading
# the agent's own recent transcript, so the notch shows what each session
# is actually about (e.g. "fix the auth bug") instead of generic
# "Session start".
#
# Output (one JSON per line):
#   {"session":"start","session_id":"<id>","source":"<claude|zcode|codex>",
#    "task":"<latest activity>","detail":"<short cwd or tool target>"}

emulate -L zsh

# JSON-escape a string (basic: backslash, quote, control chars).
json_escape() {
  python3 -c "import json,sys; print(json.dumps(sys.argv[1])[1:-1])" "$1"
}

short_cwd() {
  local d="${1:-}"
  [ -z "$d" ] && { print -r -- ""; return; }
  d="${d%/}"
  print -r -- "${d##*/}"
}

emit() {
  local id="$1" source="$2" task="$3" detail="$4" preview="${5:-}" terminal="${6:-}" last_ts="${7:-}" running="${8:-false}"
  task=$(json_escape "$task")
  detail=$(json_escape "$detail")
  preview=$(json_escape "$preview")
  terminal=$(json_escape "$terminal")
  last_ts=$(json_escape "$last_ts")
  print -r "{\"session\":\"start\",\"session_id\":\"$id\",\"source\":\"$source\",\"task\":\"$task\",\"detail\":\"$detail\",\"preview\":\"$preview\",\"terminal\":\"$terminal\",\"last_ts\":\"$last_ts\",\"running\":$running}"
}

# Check if a file was modified within the last N seconds (= agent is active).
# Returns "true" or "false".
recently_modified() {
  local file="$1"
  local secs="${2:-10}"
  [ -z "$file" ] || [ ! -f "$file" ] && { print -r -- "false"; return; }
  local mtime mod_epoch now_epoch
  mod_epoch=$(stat -f%m "$file" 2>/dev/null)
  now_epoch=$(date +%s)
  if [ -n "$mod_epoch" ] && [ $((now_epoch - mod_epoch)) -lt $secs ]; then
    print -r -- "true"
  else
    print -r -- "false"
  fi
}

# Detect the host terminal for a pid by walking parent processes until a
# known terminal .app is found. Returns lowercase short name.
# Uses python to avoid zsh subshell output leaks.
detect_terminal() {
  local pid="$1"
  python3 - "$pid" <<'PY' 2>/dev/null
import sys, subprocess
pid = int(sys.argv[1])
cur = pid
for _ in range(15):
    if not cur or cur <= 1:
        break
    try:
        cmd = subprocess.check_output(
            ["ps", "-p", str(cur), "-o", "command="],
            stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        break
    mapping = [
        ("Warp.app", "warp"), ("iTerm.app", "iterm"),
        ("Terminal.app", "terminal"), ("Hyper.app", "hyper"),
        ("Alacritty.app", "alacritty"), ("Kitty.app", "kitty"),
        ("WezTerm.app", "wezterm"), ("Ghostty.app", "ghostty"),
        ("ZCode.app", "zcode"), ("zcode", "zcode"),
        ("Cursor.app", "cursor"),
        ("VSCode.app", "vscode"), ("Visual Studio", "vscode"),
    ]
    for needle, name in mapping:
        if needle in cmd:
            print(name)
            sys.exit(0)
    try:
        ppid = int(subprocess.check_output(
            ["ps", "-p", str(cur), "-o", "ppid="],
            stderr=subprocess.DEVNULL, text=True).strip())
    except Exception:
        break
    cur = ppid
PY
}

# ── Claude Code ─────────────────────────────────────────
# claude CLI stores transcripts under ~/.claude/projects/<encoded-cwd>/<session>.jsonl
# where <encoded-cwd> is the cwd with / replaced by -.
claude_projects="$HOME/.claude/projects"

encode_cwd() {
  # Claude encodes cwd as: / replaced by -, leading / kept as -.
  print -r -- "${1//\//-}"
}

# Extract session title (FIRST user message) + latest preview (LAST
# assistant reply or tool) from a claude transcript.
# Also returns the last-activity timestamp from the transcript file's
# most recent entry, so the UI can show accurate relative time.
# Output: title<TAB>preview<TAB>iso_timestamp
claude_latest_activity() {
  local file="$1"
  [ -z "$file" ] || [ ! -f "$file" ] && { print -r -- "Claude Code		"; return; }
  python3 - "$file" <<'PY' 2>/dev/null
import json, sys
file = sys.argv[1]
first_user = ""
last_asst_text = ""
last_tool = ""
last_ts = ""
try:
    with open(file) as f:
        for line in f:
            try: d = json.loads(line)
            except: continue
            ts = d.get("timestamp") or ""
            if ts: last_ts = ts
            msg = d.get("message") or {}
            if not isinstance(msg, dict): continue
            role = msg.get("role")
            content = msg.get("content", "")
            if isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict): continue
                    if c.get("type") == "text":
                        t = c.get("text", "")
                        if role == "user" and t and not t.startswith("<"):
                            if not first_user:
                                first_user = t
                        elif role == "assistant" and t:
                            last_asst_text = t
                    elif c.get("type") == "tool_use":
                        last_tool = c.get("name", "")
            elif isinstance(content, str) and content:
                if role == "user" and not content.startswith("<"):
                    if not first_user:
                        first_user = content
                elif role == "assistant":
                    last_asst_text = content
except: pass

title = (first_user.strip().split("\n")[0][:60]) if first_user else "Claude Code"
preview = ""
if last_asst_text:
    preview = last_asst_text.strip().split("\n")[0][:70]
elif last_tool:
    preview = f"tool: {last_tool}"
print(f"{title}\t{preview}\t{last_ts}")
PY
}

ps -axo pid,command 2>/dev/null | awk '$2=="claude" && $0 !~ /grep/ {print $1}' | while read pid; do
  [ -z "$pid" ] && continue
  cwd=$(lsof -p "$pid" 2>/dev/null | awk '$4=="cwd" {print $NF; exit}')
  encoded=$(encode_cwd "$cwd")
  transcript=""
  if [ -d "$claude_projects/$encoded" ]; then
    transcript=$(ls -t "$claude_projects/$encoded"/*.jsonl 2>/dev/null | head -1)
  fi
  activity=$(claude_latest_activity "$transcript")
  # activity = title<TAB>preview<TAB>timestamp
  title=$(echo "$activity" | cut -f1)
  preview=$(echo "$activity" | cut -f2)
  last_ts=$(echo "$activity" | cut -f3)
  term=$(detect_terminal "$pid")
  running=$(recently_modified "$transcript" 15)
  emit "claude-$pid" "claude" "$title" "$(short_cwd "$cwd")" "$preview" "$term" "$last_ts" "$running"
done

# ── ZCode ───────────────────────────────────────────────
# ZCode sessions run inside ZCode.app directly (not via a long-lived
# zcode-cli process the user starts). Detect by checking if ZCode.app is
# running, then find the active session from its most recent artifacts.
zcode_artifacts="$HOME/.zcode/cli/artifacts"

# Extract title (first turn_started input) + preview (latest activity)
# + timestamp. Title comes from transcript; preview comes from the
# ROLLOUT file (which is written in real-time while the agent is working,
# so it reflects the current activity, not a stale transcript).
zcode_latest_activity() {
  local sess_dir="$1"
  local sess_id="$2"
  [ -z "$sess_id" ] && sess_id=$(basename "$sess_dir" 2>/dev/null)

  # Title from transcript (first user message = session name).
  local title="ZCode"
  local agents_dir="$HOME/.zcode/cli/agents/$sess_id"
  local transcript=""
  if [ -d "$agents_dir" ]; then
    transcript=$(find "$agents_dir" -name "transcript.jsonl" 2>/dev/null | head -1)
  fi
  if [ -n "$transcript" ] && [ -f "$transcript" ]; then
    title=$(python3 - "$transcript" <<'PY' 2>/dev/null
import json, sys
first = ""
try:
    with open(sys.argv[1]) as f:
        for line in f:
            try: d = json.loads(line)
            except: continue
            if d.get("type") == "turn_started":
                inp = d.get("payload",{}).get("input","")
                if inp and not inp.startswith("{"):
                    first = inp.strip().split("\n")[0][:60]
                    break
except: pass
print(first or "ZCode")
PY
)
  fi

  # Preview + timestamp from rollout (real-time, latest activity).
  local rollout="$HOME/.zcode/cli/rollout/model-io-$sess_id.jsonl"
  local preview="" last_ts=""
  if [ -f "$rollout" ]; then
    result=$(python3 - "$rollout" <<'PY' 2>/dev/null
import json, sys
last_content = ""
last_ts = ""
try:
    with open(sys.argv[1]) as f:
        for line in f:
            try: d = json.loads(line)
            except: continue
            # timestamp from completedAt
            ts = d.get("completedAt") or d.get("startedAt") or ""
            if ts: last_ts = ts
            resp = d.get("response", {})
            # Prefer text (assistant reply), then toolCalls (what it's doing).
            text = resp.get("text", "")
            if text:
                last_content = text.strip().split("\n")[0][:70]
            toolcalls = resp.get("toolCalls", [])
            if toolcalls and isinstance(toolcalls, list):
                tc = toolcalls[0]
                if isinstance(tc, dict):
                    name = tc.get("toolName") or tc.get("name") or ""
                    if name and not last_content:
                        last_content = f"tool: {name}"
except: pass
import sys as s2
s2.stdout.write(last_content.replace("\t"," ") + "\t" + last_ts)
PY
)
    preview=$(echo "$result" | cut -f1)
    last_ts=$(echo "$result" | cut -f2)
  fi

  print -r -- "${title}	${preview}	${last_ts}"
}

# Only emit a zcode session if ZCode.app is actually running.
if pgrep -f "ZCode.app" >/dev/null 2>&1; then
  # Find the most recently active session from artifacts dir mtime.
  recent_sess=$(ls -dt "$zcode_artifacts"/sess_* 2>/dev/null | head -1)
  sess_id=""
  if [ -n "$recent_sess" ]; then
    sess_id=$(basename "$recent_sess")
  fi
  [ -z "$sess_id" ] && sess_id="zcode-active"
  activity=$(zcode_latest_activity "$recent_sess" "$sess_id")
  title=$(echo "$activity" | cut -f1)
  preview=$(echo "$activity" | cut -f2)
  zcode_ts=$(echo "$activity" | cut -f3)
  # ZCode running: check if rollout file was modified in last 15s.
  rollout="$HOME/.zcode/cli/rollout/model-io-$sess_id.jsonl"
  running=$(recently_modified "$rollout" 15)
  emit "$sess_id" "zcode" "${title:-ZCode}" "" "${preview:-}" "zcode" "$zcode_ts" "$running"
fi

# ── Codex ───────────────────────────────────────────────
ps -axo pid,command 2>/dev/null | awk '$2=="codex" && $0 !~ /grep/ {print $1}' | while read pid; do
  [ -z "$pid" ] && continue
  cwd=$(lsof -p "$pid" 2>/dev/null | awk '$4=="cwd" {print $NF; exit}')
  emit "codex-$pid" "codex" "Codex session" "$(short_cwd "$cwd")"
done
