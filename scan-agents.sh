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

epoch_now() {
  if [ -n "${VIBE_ISLAND_NOW_EPOCH:-}" ]; then
    print -r -- "$VIBE_ISLAND_NOW_EPOCH"
  else
    date +%s
  fi
}

source_enabled() {
  local source="$1"
  local only="${VIBE_ISLAND_ONLY_SOURCES:-}"
  [ -z "$only" ] && return 0
  case ",$only," in
    *",$source,"*) return 0 ;;
    *) return 1 ;;
  esac
}

fixture_field() {
  local file="$1" pid="$2" column="$3"
  [ -z "$file" ] || [ ! -f "$file" ] && return 1
  awk -F '\t' -v target="$pid" -v col="$column" '$1 == target { print $col; exit }' "$file"
}

list_fixture_pids() {
  local file="$1"
  [ -z "$file" ] || [ ! -f "$file" ] && return 0
  awk -F '\t' 'NF { print $1 }' "$file"
}

count_lines() {
  local content="$1"
  [ -z "$content" ] && { print -r -- "0"; return; }
  print -r -- "$content" | awk 'NF { count += 1 } END { print count + 0 }'
}

recent_glob_candidates() {
  local dir="$1" pattern="$2" secs="$3" limit="${4:-0}"
  python3 - "$dir" "$pattern" "$secs" "$(epoch_now)" "$limit" <<'PY' 2>/dev/null
import glob
import os
import sys

base_dir, pattern, secs, now_epoch, limit = sys.argv[1:6]
secs = int(secs)
now_epoch = int(now_epoch)
limit = int(limit)

paths = []
glob_pattern = os.path.join(os.path.expanduser(base_dir), pattern)
for path in glob.glob(glob_pattern):
    try:
        mtime = int(os.path.getmtime(path))
    except OSError:
        continue
    if now_epoch - mtime < secs:
        paths.append((mtime, path))

paths.sort(key=lambda item: item[0], reverse=True)
if limit > 0:
    paths = paths[:limit]

for _, path in paths:
    print(path)
PY
}

process_cwd() {
  local pid="$1" fixture="${2:-}"
  local cwd=""
  cwd=$(fixture_field "$fixture" "$pid" 2)
  if [ -n "$cwd" ]; then
    print -r -- "$cwd"
  else
    lsof -p "$pid" 2>/dev/null | awk '$4=="cwd" {print $NF; exit}'
  fi
}

process_command() {
  local pid="$1" fixture="${2:-}"
  local cmd=""
  cmd=$(fixture_field "$fixture" "$pid" 3)
  if [ -n "$cmd" ]; then
    print -r -- "$cmd"
  else
    ps -p "$pid" -o command= 2>/dev/null
  fi
}

process_start_epoch() {
  local pid="$1" fixture="${2:-}"
  local start_epoch=""
  start_epoch=$(fixture_field "$fixture" "$pid" 4)
  if [ -n "$start_epoch" ]; then
    print -r -- "$start_epoch"
    return
  fi

  local etime now_epoch
  etime=$(ps -p "$pid" -o etime= 2>/dev/null | tr -d ' ')
  [ -z "$etime" ] && { print -r -- ""; return; }
  now_epoch=$(epoch_now)
  python3 - "$etime" "$now_epoch" <<'PY' 2>/dev/null
import sys

etime = sys.argv[1].strip()
now_epoch = int(sys.argv[2])

days = 0
if "-" in etime:
    day_part, etime = etime.split("-", 1)
    days = int(day_part)

parts = [int(p) for p in etime.split(":") if p]
if len(parts) == 3:
    hours, minutes, seconds = parts
elif len(parts) == 2:
    hours = 0
    minutes, seconds = parts
else:
    sys.exit(0)

elapsed = days * 86400 + hours * 3600 + minutes * 60 + seconds
print(max(0, now_epoch - elapsed))
PY
}

emit() {
  local id="$1" source="$2" task="$3" detail="$4" preview="${5:-}" terminal="${6:-}" last_ts="${7:-}" running="${8:-false}"
  local display_detail="${9:-}" cwd="${10:-}" transcript_path="${11:-}" match_confidence="${12:-}" pid="${13:-}"
  task=$(json_escape "$task")
  detail=$(json_escape "$detail")
  preview=$(json_escape "$preview")
  terminal=$(json_escape "$terminal")
  last_ts=$(json_escape "$last_ts")
  display_detail=$(json_escape "$display_detail")
  cwd=$(json_escape "$cwd")
  transcript_path=$(json_escape "$transcript_path")
  match_confidence=$(json_escape "$match_confidence")
  # pid is a bare integer (or empty); emitted so the app can focus the
  # agent's terminal even though the session_id is now a transcript UUID.
  print -r "{\"session\":\"start\",\"session_id\":\"$id\",\"source\":\"$source\",\"task\":\"$task\",\"detail\":\"$detail\",\"preview\":\"$preview\",\"terminal\":\"$terminal\",\"last_ts\":\"$last_ts\",\"running\":$running,\"display_detail\":\"$display_detail\",\"cwd\":\"$cwd\",\"transcript_path\":\"$transcript_path\",\"match_confidence\":\"$match_confidence\",\"pid\":\"$pid\"}"
}

# Check if a file was modified within the last N seconds (= agent is active).
# Returns "true" or "false".
recently_modified() {
  local file="$1"
  local secs="${2:-10}"
  [ -z "$file" ] || [ ! -f "$file" ] && { print -r -- "false"; return; }
  local mtime mod_epoch now_epoch
  mod_epoch=$(stat -f%m "$file" 2>/dev/null)
  now_epoch=$(epoch_now)
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
        ("ChatGPT.app", "chatgpt"),
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
claude_projects="${VIBE_ISLAND_CLAUDE_PROJECTS:-$HOME/.claude/projects}"

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

if source_enabled "claude"; then
  claude_process_fixture="${VIBE_ISLAND_CLAUDE_PROCESS_FIXTURE:-}"
  if [ -n "$claude_process_fixture" ]; then
    claude_pid_source=$(list_fixture_pids "$claude_process_fixture")
  else
    claude_pid_source=$(ps -axo pid,command 2>/dev/null | awk '$2=="claude" && $0 !~ /grep/ {print $1}')
  fi
  typeset -A seen_claude=()
  while read pid; do
    [ -z "$pid" ] && continue
    cwd=$(process_cwd "$pid" "$claude_process_fixture")
    encoded=$(encode_cwd "$cwd")
    transcript=""
    if [ -d "$claude_projects/$encoded" ]; then
      # (N) null-glob so an empty project dir doesn't abort with zsh's
      # "no matches found" (and an unguarded `ls -t` with no args would
      # list the cwd, producing a bogus transcript path).
      claude_files=("$claude_projects/$encoded"/*.jsonl(N))
      if [ ${#claude_files} -gt 0 ]; then
        transcript=$(ls -t -- "${claude_files[@]}" 2>/dev/null | head -1)
      fi
    fi
    [ -z "$transcript" ] && continue
    # One Claude Code session can run as several processes (main + workers)
    # that all share the same transcript. Collapse them to a single row keyed
    # by the real session UUID — which also matches the session_id the relay
    # sends, so scan and live events merge instead of duplicate.
    sess_uuid=$(basename "$transcript" .jsonl)
    (( ${+seen_claude[$sess_uuid]} )) && continue
    seen_claude[$sess_uuid]=1
    activity=$(claude_latest_activity "$transcript")
    # activity = title<TAB>preview<TAB>timestamp
    title=$(echo "$activity" | cut -f1)
    preview=$(echo "$activity" | cut -f2)
    last_ts=$(echo "$activity" | cut -f3)
    term=$(detect_terminal "$pid")
    # Only show claude if its transcript was modified recently (session
    # actually active). 5-minute window — matches codex behavior.
    is_recent=$(recently_modified "$transcript" 300)
    [ "$is_recent" = "false" ] && continue
    running=$(recently_modified "$transcript" 15)
    emit "$sess_uuid" "claude" "$title" "$cwd" "$preview" "$term" "$last_ts" "$running" "$(short_cwd "$cwd")" "$cwd" "$transcript" "cwd" "$pid"
  done <<< "$claude_pid_source"
fi

# ── ZCode ───────────────────────────────────────────────
# ZCode sessions run inside ZCode.app directly (not via a long-lived
# zcode-cli process the user starts). Detect by checking if ZCode.app is
# running, then find the active session from its most recent artifacts.
zcode_artifacts="$HOME/.zcode/cli/artifacts"

# Extract title (first turn_started input) + preview (latest activity)
# + timestamp. Title prefers the transcript; when the agents/<sess> dir is
# missing (newer ZCode versions don't always create it for main sessions),
# it falls back to the first user message embedded in the ROLLOUT file's
# request.messages. Preview always comes from the rollout (real-time).
zcode_latest_activity() {
  local sess_dir="$1"
  local sess_id="$2"
  [ -z "$sess_id" ] && sess_id=$(basename "$sess_dir" 2>/dev/null)
  local rollout="$3"
  [ -z "$rollout" ] && rollout="$HOME/.zcode/cli/rollout/model-io-$sess_id.jsonl"

  # Title from transcript (first user message = session name).
  local title=""
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
print(first)
PY
)
  fi

  # Title fallback + preview + timestamp from rollout (real-time).
  local preview="" last_ts="" rollout_title=""
  if [ -f "$rollout" ]; then
    result=$(python3 - "$rollout" <<'PY' 2>/dev/null
import json, sys

def message_text(msg):
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content
                       if isinstance(p, dict) and p.get("type") == "text")
    return ""

rollout_title = ""
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
            # First user prompt across all requests = session title. The
            # very first record is ZCode's title-generation request, whose
            # user message is exactly the raw first prompt.
            req = d.get("request", {})
            for msg in (req.get("messages") or []):
                if msg.get("role") != "user":
                    continue
                text = message_text(msg).strip()
                if not text or text.startswith(("<", "[")):
                    continue
                if not rollout_title:
                    rollout_title = text.split("\n")[0][:60]
            # Prefer text (assistant reply), then toolCalls (what it's doing).
            resp = d.get("response", {})
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
s2.stdout.write(rollout_title.replace("\t"," ") + "\t" + last_content.replace("\t"," ") + "\t" + last_ts)
PY
)
    rollout_title=$(echo "$result" | cut -f1)
    preview=$(echo "$result" | cut -f2)
    last_ts=$(echo "$result" | cut -f3)
  fi

  [ -z "$title" ] && title="$rollout_title"
  print -r -- "${title:-ZCode}	${preview}	${last_ts}"
}

# Only emit zcode sessions if ZCode.app is actually running.
# Enumerate ALL active sessions (exclude subagent sessions).
zcode_rollout_dir="${VIBE_ISLAND_ZCODE_ROLLOUT_DIR:-$HOME/.zcode/cli/rollout}"
zcode_active_window_secs="${VIBE_ISLAND_ZCODE_ACTIVE_WINDOW_SECS:-300}"
zcode_app_running_override="${VIBE_ISLAND_ZCODE_APP_RUNNING:-}"
if [ -n "$zcode_app_running_override" ]; then
  case "${zcode_app_running_override:l}" in
    1|true|yes) zcode_app_running=true ;;
    *) zcode_app_running=false ;;
  esac
elif pgrep -f "ZCode.app" >/dev/null 2>&1; then
  zcode_app_running=true
else
  zcode_app_running=false
fi

if source_enabled "zcode" && [ "$zcode_app_running" = "true" ]; then
  recent_zcode_rollouts=$(recent_glob_candidates "$zcode_rollout_dir" "model-io-sess_*.jsonl" "$zcode_active_window_secs" 5)
  for rf in ${(f)recent_zcode_rollouts}; do
    [[ "$rf" == *subagent* ]] && continue
    sess_id=$(basename "$rf" .jsonl | sed 's/model-io-//')
    [ -z "$sess_id" ] && continue
    rollout="$zcode_rollout_dir/model-io-$sess_id.jsonl"
    activity=$(zcode_latest_activity "" "$sess_id" "$rollout")
    title=$(echo "$activity" | cut -f1)
    preview=$(echo "$activity" | cut -f2)
    zcode_ts=$(echo "$activity" | cut -f3)
    running=$(recently_modified "$rollout" 15)
    emit "$sess_id" "zcode" "${title:-ZCode}" "" "${preview:-}" "zcode" "$zcode_ts" "$running" "" "" "$rollout" "recent_rollout"
  done
fi

# Pick the most likely Codex transcript for a pid.
# Prints: transcript_path<TAB>match_confidence<TAB>session_kind<TAB>session_id
codex_select_transcript() {
  local cwd="$1" process_start="$2" claimed_file="$3"
  [ -z "$cwd" ] && { print -r -- ""; return; }
  python3 - "$codex_sessions_dir" "$cwd" "$process_start" "$claimed_file" "$(epoch_now)" <<'PY' 2>/dev/null
import datetime as dt
import glob
import json
import os
import sys

sessions_dir, target_cwd, process_start, claimed_file, now_epoch = sys.argv[1:6]
process_start = int(process_start) if process_start else None
now_epoch = int(now_epoch)

claimed = set()
if claimed_file and os.path.exists(claimed_file):
    with open(claimed_file) as fh:
        claimed = {line.strip() for line in fh if line.strip()}

def parse_iso(value):
    if not value:
        return None
    try:
        return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None

candidates = []
pattern = os.path.join(os.path.expanduser(sessions_dir), "**", "rollout-*.jsonl")
for path in glob.glob(pattern, recursive=True):
    if path in claimed:
        continue
    try:
        mtime = int(os.path.getmtime(path))
    except OSError:
        continue
    if now_epoch - mtime >= 300:
        continue

    meta = None
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if record.get("type") == "session_meta":
                    payload = record.get("payload") or {}
                    meta = {
                        "cwd": payload.get("cwd") or "",
                        "session_id": payload.get("id") or "",
                        "source": payload.get("source"),
                        "timestamp": parse_iso(payload.get("timestamp")) or parse_iso(record.get("timestamp")),
                    }
                    break
    except OSError:
        continue

    if not meta or meta["cwd"] != target_cwd:
        continue

    source = meta["source"]
    if isinstance(source, dict) and source.get("subagent"):
        session_kind = "subagent"
    else:
        session_kind = "top_level"

    delta = abs(meta["timestamp"] - process_start) if (process_start is not None and meta["timestamp"] is not None) else None
    candidates.append({
        "path": path,
        "mtime": mtime,
        "delta": delta,
        "session_id": meta["session_id"],
        "session_kind": session_kind,
    })

if not candidates:
    sys.exit(0)

if process_start is None:
    if len(candidates) != 1:
        sys.exit(0)
    chosen = candidates[0]
    confidence = "only_candidate"
else:
    sortable = [c for c in candidates if c["delta"] is not None]
    if not sortable:
        sys.exit(0)
    sortable.sort(key=lambda c: (c["delta"], -c["mtime"]))
    chosen = sortable[0]
    if chosen["delta"] > 180:
        sys.exit(0)
    if len(sortable) > 1 and sortable[1]["delta"] - chosen["delta"] < 5:
        sys.exit(0)
    confidence = "start_window" if chosen["delta"] <= 30 else "start_time"

print(f"{chosen['path']}\t{confidence}\t{chosen['session_kind']}\t{chosen['session_id']}")
PY
}

# ── Codex (OpenAI Codex CLI) ────────────────────────────
# Codex stores sessions under ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
# Each line: {type: "session_meta"|"event_msg", payload:{...}, timestamp:"..."}
codex_sessions_dir="${VIBE_ISLAND_CODEX_SESSIONS_DIR:-$HOME/.codex/sessions}"
codex_latest_activity() {
  local file="$1"
  [ -z "$file" ] || [ ! -f "$file" ] && { print -r -- "Codex			"; return; }
  python3 - "$file" <<'PY' 2>/dev/null
import json, sys
title = ""
last_agent_msg = ""
last_ts = ""
try:
    with open(sys.argv[1]) as f:
        for line in f:
            try: d = json.loads(line)
            except: continue
            ts = d.get("timestamp", "")
            if ts: last_ts = ts
            t = d.get("type", "")
            p = d.get("payload", {})
            if t == "session_meta":
                if not title:
                    title = (p.get("title") or p.get("instructions") or "")[:60]
            elif t == "event_msg":
                ev = p.get("type", "")
                if ev == "user_message" and not title:
                    msg = p.get("message", "")
                    if isinstance(msg, str):
                        title = msg.strip().split("\n")[0][:60]
                elif ev == "agent_message":
                    msg = p.get("message", "")
                    if isinstance(msg, str) and msg.strip():
                        last_agent_msg = msg.strip().split("\n")[0][:70]
except: pass
title = title or "Codex"
print(f"{title}\t{last_agent_msg}\t{last_ts}")
PY
}

if source_enabled "codex"; then

  # Scan ~/.codex/sessions for recently-active rollout files not already
  # claimed by the PID scan. Returns one line per top-level session:
  #   path<TAB>cwd<TAB>session_id<TAB>title<TAB>preview<TAB>last_ts
  codex_scan_rollouts() {
    local claimed="$1"
    python3 - "$codex_sessions_dir" "$claimed" "$(epoch_now)" <<'PY' 2>/dev/null
import datetime as dt
import glob
import json
import os
import sys

sessions_dir, claimed_file, now_epoch = sys.argv[1:4]
now_epoch = int(now_epoch)
active_window = 600  # 10 min — broader than PID scan's 5 min

claimed = set()
if claimed_file and os.path.exists(claimed_file):
    with open(claimed_file) as fh:
        claimed = {line.strip() for line in fh if line.strip()}


def parse_iso(value):
    if not value:
        return None
    try:
        return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


seen = {}  # session_id -> entry dict (most-recent-mtime wins)
pattern = os.path.join(os.path.expanduser(sessions_dir), "**", "rollout-*.jsonl")
for path in glob.glob(pattern, recursive=True):
    if path in claimed:
        continue
    try:
        mtime = int(os.path.getmtime(path))
    except OSError:
        continue
    if now_epoch - mtime >= active_window:
        continue

    meta = None
    title = ""
    last_agent_msg = ""
    last_ts = ""
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                ts = d.get("timestamp", "")
                if ts:
                    last_ts = ts
                t = d.get("type", "")
                p = d.get("payload", {})
                if t == "session_meta":
                    meta = {
                        "cwd": p.get("cwd") or "",
                        "session_id": p.get("id") or "",
                        "source": p.get("source"),
                    }
                    if not title:
                        title = (p.get("title") or p.get("instructions") or "")[:60]
                elif t == "event_msg":
                    ev = p.get("type", "")
                    if ev == "user_message" and not title:
                        msg = p.get("message", "")
                        if isinstance(msg, str):
                            title = msg.strip().split("\n")[0][:60]
                    elif ev == "agent_message":
                        msg = p.get("message", "")
                        if isinstance(msg, str) and msg.strip():
                            last_agent_msg = msg.strip().split("\n")[0][:70]
    except OSError:
        continue

    if not meta or not meta["session_id"] or not meta["cwd"]:
        continue
    # Skip subagent sessions — only surface top-level conversations.
    src = meta["source"]
    if isinstance(src, dict):
        if src.get("subagent") or src.get("thread_spawn") or src.get("other"):
            continue

    sid = meta["session_id"]
    if sid in seen and seen[sid]["mtime"] >= mtime:
        continue
    seen[sid] = {
        "path": path,
        "mtime": mtime,
        "cwd": meta["cwd"],
        "title": title or "Codex",
        "preview": last_agent_msg,
        "last_ts": last_ts,
        "session_id": sid,
    }

for entry in seen.values():
    print(f"{entry['path']}\t{entry['cwd']}\t{entry['session_id']}\t{entry['title']}\t{entry['preview']}\t{entry['last_ts']}")
PY
  }

  codex_process_fixture="${VIBE_ISLAND_CODEX_PROCESS_FIXTURE:-}"
  if [ -n "$codex_process_fixture" ]; then
    codex_pid_source=$(list_fixture_pids "$codex_process_fixture")
  else
    codex_pid_source=$(ps -axo pid,comm 2>/dev/null | grep '/codex$' | awk '{print $1}')
  fi

  codex_claimed=$(mktemp "/tmp/vibe-island-codex-claimed.XXXXXX")
  trap 'rm -f "$codex_claimed"' EXIT

  # Match codex CLI user sessions. On macOS `ps -o comm` gives the full
  # executable path, so we grep for processes whose executable ends with
  # /codex, then filter out known helper/worker patterns.
  while read pid; do
    [ -z "$pid" ] && continue
    cmd=$(process_command "$pid" "$codex_process_fixture")
    # Skip known helpers (NOT app-server — codex always runs in that mode).
    case "$cmd" in
      *chrome*|*plugins*|*computer-use*|*Sparkle*|*"Codex (Service)"*|*node_repl*|*injector*) continue ;;
    esac
    cwd=$(process_cwd "$pid" "$codex_process_fixture")
    start_epoch=$(process_start_epoch "$pid" "$codex_process_fixture")
    selected=$(codex_select_transcript "$cwd" "$start_epoch" "$codex_claimed")
    transcript=$(echo "$selected" | cut -f1)
    match_confidence=$(echo "$selected" | cut -f2)
    session_id=$(echo "$selected" | cut -f4)
    [ -z "$transcript" ] && continue
    [ -z "$session_id" ] && continue
    print -r -- "$transcript" >> "$codex_claimed"

   activity=$(codex_latest_activity "$transcript")
   title=$(echo "$activity" | cut -f1)
   preview=$(echo "$activity" | cut -f2)
   last_ts=$(echo "$activity" | cut -f3)
   term=$(detect_terminal "$pid")
   [ -z "$term" ] && case "$cmd" in *ChatGPT.app*) term="chatgpt" ;; esac
   running=$(recently_modified "$transcript" 15)
   emit "$session_id" "codex" "${title:-Codex}" "$(short_cwd "$cwd")" "$preview" "$term" "$last_ts" "$running" "$(short_cwd "$cwd")" "$cwd" "$transcript" "$match_confidence" "$pid"
 done <<< "$codex_pid_source"

  # Rollout-based fallback: scan ~/.codex/sessions for recently-active
  # top-level sessions that the PID scan did not find. This is how ChatGPT
  # desktop App Codex sessions surface — their runtime process is node_repl,
  # not a CLI /codex binary, and their sessions are long-lived so start-time
  # matching fails. Sessions already claimed by the PID scan are skipped.
  codex_rollout_results=$(codex_scan_rollouts "$codex_claimed")
  for rr in ${(f)codex_rollout_results}; do
    [ -z "$rr" ] && continue
    r_transcript=$(echo "$rr" | cut -f1)
    r_cwd=$(echo "$rr" | cut -f2)
    r_session_id=$(echo "$rr" | cut -f3)
    r_title=$(echo "$rr" | cut -f4)
    r_preview=$(echo "$rr" | cut -f5)
    r_last_ts=$(echo "$rr" | cut -f6)
    [ -z "$r_session_id" ] && continue
    r_short=$(short_cwd "$r_cwd")
    r_running=$(recently_modified "$r_transcript" 15)
    emit "$r_session_id" "codex" "${r_title:-Codex}" "$r_short" "$r_preview" "" "$r_last_ts" "$r_running" "$r_short" "$r_cwd" "$r_transcript" "rollout_scan" ""
  done

  rm -f "$codex_claimed"
  trap - EXIT
fi

# ── Gemini / Antigravity CLI ────────────────────────────
# Antigravity stores conversation metadata + summaries in:
#   ~/.gemini/antigravity-cli/cache/conversation_metadata.json
#   ~/.gemini/antigravity-cli/cache/last_conversations.json
gemini_log_dir="${VIBE_ISLAND_GEMINI_LOG_DIR:-$HOME/.gemini/antigravity-cli/log}"
gemini_db_file="${VIBE_ISLAND_GEMINI_DB_FILE:-$HOME/.gemini/antigravity-cli/conversation_summaries.db}"
gemini_active_window_secs="${VIBE_ISLAND_GEMINI_ACTIVE_WINDOW_SECS:-300}"
antigravity_latest_activity() {
  local log_file="$1" db_file="$2"
  # Priority 1: parse the CLI log for the current conversation ID + latest
  # user message (most accurate — reflects what's happening right now).
  if [ -n "$log_file" ] && [ -f "$log_file" ]; then
    result=$(python3 - "$log_file" <<'PY' 2>/dev/null
import re, sys, os
log_file = sys.argv[1]
current_conv = ""
last_user_msg = ""
last_ts = ""
try:
    with open(log_file) as f:
        lines = f.readlines()
    for line in lines:
        # Extract conversation ID from "Sending user message to conversation XXX"
        m = re.search(r"conversation ([a-f0-9-]+)", line)
        if m:
            current_conv = m.group(1)
        # Extract timestamp from log line prefix (I0722 20:44:10.448)
        ts_m = re.match(r"[IWE](\d{4}) (\d{2}:\d{2}:\d{2})", line)
        if ts_m:
            last_ts = ts_m.group(1) + " " + ts_m.group(2)
    # last_ts is relative to today; convert to approximate ISO.
    if last_ts:
        import datetime
        today = datetime.date.today()
        # last_ts format: "0722 20:44:10"
        mm = int(last_ts[:2])
        dd = int(last_ts[2:4])
        time_part = last_ts[5:]
        try:
            dt = datetime.datetime(today.year, mm, dd,
                                   int(time_part[:2]), int(time_part[3:5]), int(time_part[6:8]))
            last_ts = dt.isoformat()
        except:
            pass
except: pass

# We can't easily extract the actual user message text from the log,
# so fall back to metadata for the title/preview.
print(f"{current_conv}\t{last_ts}")
PY
)
    current_conv=$(echo "$result" | cut -f1)
    log_ts=$(echo "$result" | cut -f2)
  fi

  # Priority 2: get title/preview from conversation summaries DB using the
  # conversation ID found in the log. If the current conversation isn't in
  # the DB yet (too new), show "Antigravity" rather than a stale old session.
  local title="Antigravity" preview=""
  if [ -n "$current_conv" ] && [ -f "$db_file" ]; then
    result=$(python3 - "$db_file" "$current_conv" <<'PY' 2>/dev/null
import sqlite3, sys
db_file, conv_id = sys.argv[1], sys.argv[2]
title = "Antigravity"
preview = ""
try:
    conn = sqlite3.connect(db_file)
    row = conn.execute(
        "SELECT title, preview FROM conversation_summaries WHERE conversation_id = ?",
        (conv_id,)).fetchone()
    if row:
        title = (row[0] or row[1] or "Antigravity")[:60]
        preview = (row[1] or "")[:70]
    conn.close()
except: pass
print(f"{title}\t{preview}")
PY
)
    title=$(echo "$result" | cut -f1)
    preview=$(echo "$result" | cut -f2)
  fi

  print -r -- "${title}	${preview}	${log_ts}"
}

# Gemini / Antigravity CLI (process name: agy-bin or agy).
# Antigravity is Google's Gemini-powered coding agent.
if source_enabled "gemini"; then
  gemini_process_fixture="${VIBE_ISLAND_GEMINI_PROCESS_FIXTURE:-}"
  if [ -n "$gemini_process_fixture" ]; then
    gemini_pids=$(list_fixture_pids "$gemini_process_fixture")
  else
    gemini_pids=$(ps -axo pid,comm 2>/dev/null | grep -E '/(agy-bin|agy|gemini)$' | awk '{print $1}')
  fi

  gemini_pid_count=$(count_lines "$gemini_pids")
  gemini_recent_logs=$(recent_glob_candidates "$gemini_log_dir" "cli-*.log" "$gemini_active_window_secs" 2)
  gemini_log_count=$(count_lines "$gemini_recent_logs")
  if [ "$gemini_pid_count" -eq 1 ] && [ "$gemini_log_count" -eq 1 ]; then
    pid=$(print -r -- "$gemini_pids" | awk 'NF { print; exit }')
    cwd=$(process_cwd "$pid" "$gemini_process_fixture")
    latest_log=$(print -r -- "$gemini_recent_logs" | awk 'NF { print; exit }')
    activity=$(antigravity_latest_activity "$latest_log" "$gemini_db_file")
    title=$(echo "$activity" | cut -f1)
    preview=$(echo "$activity" | cut -f2)
    last_ts=$(echo "$activity" | cut -f3)
    term=$(detect_terminal "$pid")
    running=$(recently_modified "$latest_log" 15)
    emit "gemini-$pid" "gemini" "${title:-Antigravity}" "$(short_cwd "$cwd")" "$preview" "$term" "$last_ts" "$running" "$(short_cwd "$cwd")" "$cwd" "$latest_log" "single_recent_log"
  fi
fi

# ── DeepSeek ────────────────────────────────────────────
# DeepSeek stores sessions as ~/.deepseek/sessions/<id>.json
# Each is {messages:[{role,content}], metadata:{...}, system_prompt:"..."}
deepseek_sessions_dir="${VIBE_ISLAND_DEEPSEEK_SESSIONS_DIR:-$HOME/.deepseek/sessions}"
deepseek_active_window_secs="${VIBE_ISLAND_DEEPSEEK_ACTIVE_WINDOW_SECS:-300}"
deepseek_latest_activity() {
  local file="$1"
  [ -z "$file" ] || [ ! -f "$file" ] && { print -r -- "DeepSeek			"; return; }
  python3 - "$file" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    msgs = d.get("messages", [])
    title = "DeepSeek"
    preview = ""
    for m in msgs:
        role = m.get("role","")
        content = m.get("content","")
        if isinstance(content, list):
            content = " ".join(c.get("text","") for c in content if isinstance(c,dict))
        if role == "user" and content and title == "DeepSeek":
            title = content.strip().split("\n")[0][:60]
        elif role == "assistant" and content:
            preview = content.strip().split("\n")[0][:70]
    meta = d.get("metadata", {})
    ts = meta.get("updatedAt") or meta.get("createdAt") or ""
    print(f"{title}\t{preview}\t{ts}")
except:
    print("DeepSeek\t\t")
PY
}

if source_enabled "deepseek"; then
  deepseek_process_fixture="${VIBE_ISLAND_DEEPSEEK_PROCESS_FIXTURE:-}"
  if [ -n "$deepseek_process_fixture" ]; then
    deepseek_pids=$(list_fixture_pids "$deepseek_process_fixture")
  else
    deepseek_pids=$(ps -axo pid,comm 2>/dev/null | grep '/deepseek$' | awk '{print $1}')
  fi

  deepseek_pid_count=$(count_lines "$deepseek_pids")
  deepseek_recent_sessions=$(recent_glob_candidates "$deepseek_sessions_dir" "*.json" "$deepseek_active_window_secs" 2)
  deepseek_session_count=$(count_lines "$deepseek_recent_sessions")
  if [ "$deepseek_pid_count" -eq 1 ] && [ "$deepseek_session_count" -eq 1 ]; then
    pid=$(print -r -- "$deepseek_pids" | awk 'NF { print; exit }')
    cwd=$(process_cwd "$pid" "$deepseek_process_fixture")
    latest_session=$(print -r -- "$deepseek_recent_sessions" | awk 'NF { print; exit }')
    activity=$(deepseek_latest_activity "$latest_session")
    title=$(echo "$activity" | cut -f1)
    preview=$(echo "$activity" | cut -f2)
    last_ts=$(echo "$activity" | cut -f3)
    term=$(detect_terminal "$pid")
    running=$(recently_modified "$latest_session" 15)
    emit "deepseek-$pid" "deepseek" "${title:-DeepSeek}" "$(short_cwd "$cwd")" "$preview" "$term" "$last_ts" "$running" "$(short_cwd "$cwd")" "$cwd" "$latest_session" "single_recent_session"
  fi
fi

# ── Generic process helper for file-based agents ────────
# Match a running agent CLI by scanning ps command fields for the binary
# name (works for direct binaries AND node/npm shim invocations like
# `node /path/bin/gemini`). Fixture file overrides ps for tests.
agent_process_pids() {
  local name="$1" fixture="${2:-}"
  if [ -n "$fixture" ]; then
    list_fixture_pids "$fixture"
    return
  fi
  ps -axo pid,command 2>/dev/null | awk -v n="$name" '
    /awk -v n=/ { next }
    {
      for (i = 2; i <= NF; i++) {
        if ($i == n || $i ~ ("/" n "$")) { print $1; break }
      }
    }'
}

# ── Native Gemini CLI / Qwen Code ────────────────────────
# Google's gemini CLI (and its fork qwen-code) store chats at
#   <tmp>/<project-slug>/chats/session-<date>-<id>.jsonl
# with a .project_root file next to chats/ holding the real cwd, and a
# header line {sessionId, startTime, lastUpdated} on line 1. Records:
#   {type:"user", timestamp, content:[{text}]}
#   {type:"gemini", timestamp, content:"...", toolCalls:[...]}
gemini_cli_chats_activity() {
  local file="$1"
  [ -z "$file" ] || [ ! -f "$file" ] && { print -r -- "			"; return; }
  python3 - "$file" <<'PY' 2>/dev/null
import json, sys

def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""

title = ""
preview = ""
last_ts = ""
try:
    with open(sys.argv[1]) as f:
        for line in f:
            try: d = json.loads(line)
            except: continue
            if not isinstance(d, dict): continue
            t = d.get("type", "")
            ts = d.get("timestamp") or ""
            if ts: last_ts = ts
            if t == "user":
                txt = text_of(d.get("content"))
                if txt and not title and not txt.lstrip().startswith(("<", "[")):
                    title = txt.strip().split("\n")[0][:60]
            elif t in ("gemini", "model", "assistant"):
                txt = text_of(d.get("content"))
                if txt:
                    preview = txt.strip().split("\n")[0][:70]
                else:
                    tcs = d.get("toolCalls") or []
                    if isinstance(tcs, list) and tcs and isinstance(tcs[0], dict):
                        fn = tcs[0].get("name") or (tcs[0].get("function") or {}).get("name") or ""
                        if fn:
                            preview = f"tool: {fn}"
except: pass
print(f"{title}\t{preview}\t{last_ts}")
PY
}

# Shared scanner for chats-style agents (native gemini CLI, qwen).
# Usage: scan_chats_style_agent <source> <tmp_dir> <window_secs> <fixture> <proc_name> <default_title>
scan_chats_style_agent() {
  local source="$1" tmp_dir="$2" window="${3:-300}" fixture="${4:-}" proc_name="$5" default_title="$6"
  [ -d "$tmp_dir" ] || return 0
  local recent
  recent=$(recent_glob_candidates "$tmp_dir" "*/chats/session-*.jsonl" "$window")
  [ -z "$recent" ] && return 0

  # Attach a pid only when exactly one agent process runs, so the notch
  # can still focus the right terminal.
  local pids pid_count pid term=""
  pids=$(agent_process_pids "$proc_name" "$fixture")
  pid_count=$(count_lines "$pids")
  pid=""
  if [ "$pid_count" -eq 1 ]; then
    pid=$(print -r -- "$pids" | awk 'NF { print; exit }')
    term=$(detect_terminal "$pid")
  fi

  local f activity title preview last_ts cwd sess_id running slug_dir
  for f in ${(f)recent}; do
    activity=$(gemini_cli_chats_activity "$f")
    title=$(echo "$activity" | cut -f1)
    preview=$(echo "$activity" | cut -f2)
    last_ts=$(echo "$activity" | cut -f3)
    slug_dir=$(dirname "$(dirname "$f")")
    cwd=$(cat "$slug_dir/.project_root" 2>/dev/null)
    sess_id=$(python3 -c "import json,sys; print(json.loads(open(sys.argv[1]).readline()).get('sessionId',''))" "$f" 2>/dev/null)
    [ -z "$sess_id" ] && sess_id=$(basename "$f" .jsonl)
    running=$(recently_modified "$f" 15)
    emit "$sess_id" "$source" "${title:-$default_title}" "$(short_cwd "$cwd")" "$preview" "$term" "$last_ts" "$running" "$(short_cwd "$cwd")" "$cwd" "$f" "recent_chat" "$pid"
  done
}

if source_enabled "gemini"; then
  scan_chats_style_agent "gemini" \
    "${VIBE_ISLAND_GEMINI_CLI_TMP_DIR:-$HOME/.gemini/tmp}" \
    "${VIBE_ISLAND_GEMINI_CLI_ACTIVE_WINDOW_SECS:-300}" \
    "${VIBE_ISLAND_GEMINI_CLI_PROCESS_FIXTURE:-}" \
    "gemini" "Gemini CLI"
fi

if source_enabled "qwen"; then
  scan_chats_style_agent "qwen" \
    "${VIBE_ISLAND_QWEN_TMP_DIR:-$HOME/.qwen/tmp}" \
    "${VIBE_ISLAND_QWEN_ACTIVE_WINDOW_SECS:-300}" \
    "${VIBE_ISLAND_QWEN_PROCESS_FIXTURE:-}" \
    "qwen" "Qwen Code"
fi

# ── Kimi CLI (Moonshot) ──────────────────────────────────
# Kimi Code CLI stores wire logs at either
#   ~/.kimi/sessions/<group>/<session>/wire.jsonl            or
#   ~/.kimi-code/sessions/<ws>/<session>/agents/<id>/wire.jsonl
# The wire format is not a stable public contract, so parsing is
# intentionally lenient (role/type hints + recursive text extraction).
kimi_wire_activity() {
  local file="$1"
  [ -z "$file" ] || [ ! -f "$file" ] && { print -r -- "			"; return; }
  python3 - "$file" <<'PY' 2>/dev/null
import json, sys

def walk_text(node):
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        for key in ("text", "content", "input", "message"):
            value = node.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, (dict, list)):
                got = walk_text(value)
                if got:
                    return got
    if isinstance(node, list):
        for item in node:
            got = walk_text(item)
            if got:
                return got
    return ""

def role_of(d):
    for key in ("role", "type", "speaker"):
        value = d.get(key)
        if isinstance(value, str) and value:
            return value.lower()
    return ""

title = ""
preview = ""
last_ts = ""
try:
    with open(sys.argv[1]) as f:
        for line in f:
            try: d = json.loads(line)
            except: continue
            if not isinstance(d, dict): continue
            ts = d.get("timestamp") or d.get("time") or ""
            if isinstance(ts, str) and ts: last_ts = ts
            nested = d.get("record") if isinstance(d.get("record"), dict) else d
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            role = role_of(d) or role_of(nested) or role_of(msg)
            if role in ("user", "human") and not title:
                txt = walk_text(d)
                if txt and not txt.lstrip().startswith(("<", "[")):
                    title = txt.strip().split("\n")[0][:60]
            elif role in ("assistant", "agent", "kimi", "ai", "model"):
                txt = walk_text(d)
                if txt:
                    preview = txt.strip().split("\n")[0][:70]
except: pass
print(f"{title}\t{preview}\t{last_ts}")
PY
}

if source_enabled "kimi"; then
  kimi_window="${VIBE_ISLAND_KIMI_ACTIVE_WINDOW_SECS:-300}"
  kimi_fixture="${VIBE_ISLAND_KIMI_PROCESS_FIXTURE:-}"
  kimi_roots="${VIBE_ISLAND_KIMI_SESSIONS_DIR:-$HOME/.kimi/sessions:$HOME/.kimi-code/sessions}"
  typeset -A seen_kimi=()
  for root in ${(s.:.)kimi_roots}; do
    [ -d "$root" ] || continue
    # Both layouts: <root>/<g>/<session>/wire.jsonl and
    # <root>/<ws>/<session>/agents/<agent>/wire.jsonl
    kimi_wires=$(recent_glob_candidates "$root" "*/*/wire.jsonl" "$kimi_window")
    kimi_wires="$kimi_wires
$(recent_glob_candidates "$root" "*/*/*/*/wire.jsonl" "$kimi_window")"
    for wf in ${(f)kimi_wires}; do
      [ -z "$wf" ] && continue
      # Session dir is the component before an optional agents/<agent> tail.
      sess_dir_name=$(print -r -- "${wf#$root/}" | awk -F/ '{ if ($(NF-2)=="agents") print $(NF-3); else print $(NF-1) }')
      [ -z "$sess_dir_name" ] && continue
      (( ${+seen_kimi[$sess_dir_name]} )) && continue
      seen_kimi[$sess_dir_name]=1
      activity=$(kimi_wire_activity "$wf")
      k_title=$(echo "$activity" | cut -f1)
      k_preview=$(echo "$activity" | cut -f2)
      k_ts=$(echo "$activity" | cut -f3)
      running=$(recently_modified "$wf" 15)
      emit "$sess_dir_name" "kimi" "${k_title:-Kimi}" "" "$k_preview" "" "$k_ts" "$running" "" "" "$wf" "recent_wire" ""
    done
  done
fi

# ── OpenCode ─────────────────────────────────────────────
# OpenCode shards its data under ~/.local/share/opencode/storage:
#   session/<project-hash|global>/<id>.json   {id,title,parentID,directory,time}
#   message/<session-id>/<msg-id>.json        {id,role,sessionID,time,parts?}
#   message_part/<session-id>/<msg-id>/*.json {type:"text",text}
# Subagent sessions carry a parentID and are skipped.
if source_enabled "opencode"; then
  opencode_storage="${VIBE_ISLAND_OPENCODE_STORAGE_DIR:-$HOME/.local/share/opencode/storage}"
  opencode_window="${VIBE_ISLAND_OPENCODE_ACTIVE_WINDOW_SECS:-300}"
  if [ -d "$opencode_storage" ]; then
    opencode_rows=$(python3 - "$opencode_storage" "$(epoch_now)" "$opencode_window" <<'PY' 2>/dev/null
import glob, json, os, sys

storage = os.path.expanduser(sys.argv[1])
now_epoch = int(sys.argv[2])
window = int(sys.argv[3])

def part_text(storage, sid, mid):
    for part_root in (os.path.join(storage, "message_part", sid, mid),
                      os.path.join(storage, "part", mid)):
        for pp in sorted(glob.glob(os.path.join(part_root, "*.json")),
                         key=os.path.getmtime, reverse=True)[:4]:
            try:
                with open(pp) as fh:
                    pd = json.load(fh)
            except Exception:
                continue
            if not isinstance(pd, dict) or pd.get("type") != "text":
                continue
            blob = pd.get("text")
            if not blob and isinstance(pd.get("data"), dict):
                blob = pd["data"].get("text")
            if isinstance(blob, str) and blob.strip():
                return blob.strip()
    return ""

def message_text(storage, sid, m):
    if isinstance(m.get("parts"), list):
        text = " ".join(str(p.get("text") or "") for p in m["parts"]
                        if isinstance(p, dict) and p.get("type") == "text").strip()
        if text:
            return text
    return part_text(storage, sid, str(m.get("id") or ""))

rows = []
pattern = os.path.join(storage, "session", "*", "*.json")
for path in glob.glob(pattern):
    try:
        mtime = int(os.path.getmtime(path))
    except OSError:
        continue
    if now_epoch - mtime >= window:
        continue
    try:
        with open(path) as fh:
            d = json.load(fh)
    except Exception:
        continue
    if not isinstance(d, dict) or d.get("parentID"):
        continue
    sid = str(d.get("id") or os.path.basename(path)[:-5])
    cwd = str(d.get("directory") or d.get("cwd") or "")
    t = d.get("time") if isinstance(d.get("time"), dict) else {}
    last_ts = str(t.get("updated") or t.get("end") or t.get("created") or "")

    msgs = []
    msg_dir = os.path.join(storage, "message", sid)
    if os.path.isdir(msg_dir):
        for mp in glob.glob(os.path.join(msg_dir, "*.json")):
            try:
                msgs.append((int(os.path.getmtime(mp)), mp))
            except OSError:
                continue
    msgs.sort()

    running = False
    title = str(d.get("title") or "").strip()
    preview = ""
    first_user = ""
    newest_mtime = mtime
    if msgs:
        newest_mtime = msgs[-1][0]
        running = (now_epoch - newest_mtime) < 15
        loaded = []
        for mm, mp in msgs:
            try:
                with open(mp) as fh:
                    m = json.load(fh)
            except Exception:
                continue
            if isinstance(m, dict):
                loaded.append(m)
        if not title:
            for m in loaded:  # oldest → newest: first user prompt
                if str(m.get("role") or "") == "user":
                    text = message_text(storage, sid, m)
                    if text and not text.lstrip().startswith(("<", "[")):
                        first_user = text.split("\n")[0][:60]
                        break
        for m in reversed(loaded):  # newest → oldest: last assistant text
            if str(m.get("role") or "") == "assistant":
                text = message_text(storage, sid, m)
                if text:
                    preview = text.split("\n")[0][:70]
                    break

    print("\t".join([sid, title or first_user, preview, last_ts, cwd, path,
                     "true" if running else "false"]))
PY
)
    # Attach a pid only when exactly one opencode process runs.
    opencode_pids=$(agent_process_pids "opencode" "${VIBE_ISLAND_OPENCODE_PROCESS_FIXTURE:-}")
    oc_pid=""
    oc_term=""
    if [ "$(count_lines "$opencode_pids")" -eq 1 ]; then
      oc_pid=$(print -r -- "$opencode_pids" | awk 'NF { print; exit }')
      oc_term=$(detect_terminal "$oc_pid")
    fi
    for orow in ${(f)opencode_rows}; do
      [ -z "$orow" ] && continue
      o_sid=$(echo "$orow" | cut -f1)
      o_title=$(echo "$orow" | cut -f2)
      o_preview=$(echo "$orow" | cut -f3)
      o_ts=$(echo "$orow" | cut -f4)
      o_cwd=$(echo "$orow" | cut -f5)
      o_path=$(echo "$orow" | cut -f6)
      o_running=$(echo "$orow" | cut -f7)
      [ "$o_running" = "true" ] || o_running="false"
      emit "$o_sid" "opencode" "${o_title:-OpenCode}" "$(short_cwd "$o_cwd")" "$o_preview" "$oc_term" "$o_ts" "$o_running" "$(short_cwd "$o_cwd")" "$o_cwd" "$o_path" "recent_session" "$oc_pid"
    done
  fi
fi
