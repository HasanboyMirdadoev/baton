#!/bin/bash
# gem — run and control Antigravity CLI (agy) sessions the way Claude controls subagents.
#
#   gem start <name> [--cwd DIR] [--mode edit|plan|yolo] [--model M] [--file F | PROMPT]
#   gem send  <name> [--file F | PROMPT]     continue an existing session (same context)
#   gem watch [name]                         live view of one session, or all of them
#   gem result <name> [TURN]                 print the agent's reply (last turn by default)
#   gem status <name>                        running | done | error | stopped
#   gem list                                 all sessions
#   gem models                               models the signed-in account can use
#   gem stop <name>                          kill a running turn
#   gem close <name>                         stop and delete the session's local state
#
# start/send block until the agent finishes and then print its reply, so run them
# with Bash run_in_background to get notified on completion.
# State lives in ~/.claude/gemini-sessions/<name>/.

set -uo pipefail
ROOT="${GEM_HOME:-$HOME/.claude/gemini-sessions}"
SCRIPTS="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
PREAMBLE_FILE="$SCRIPTS/../preamble.md"
AGY="${GEM_AGY:-$HOME/.local/bin/agy}"
mkdir -p "$ROOT"

die() { echo "gem: $*" >&2; exit 1; }
sdir() { echo "$ROOT/$1"; }
need_session() { [ -f "$(sdir "$1")/meta" ] || die "no session '$1' (see: gem list)"; }
meta_get() { grep -m1 "^$2=" "$(sdir "$1")/meta" | cut -d= -f2-; }
meta_set() {
  local f; f="$(sdir "$1")/meta"
  grep -v "^$2=" "$f" > "$f.tmp" 2>/dev/null; echo "$2=$3" >> "$f.tmp"; mv "$f.tmp" "$f"
}
is_running() {
  local pid; pid=$(cat "$(sdir "$1")/pid" 2>/dev/null) || return 1
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

# Read the prompt from --file F, the remaining args, or stdin.
read_prompt() {
  if [ "${1:-}" = "--file" ]; then cat "$2"
  elif [ $# -gt 0 ]; then printf '%s' "$*"
  elif [ ! -t 0 ]; then cat
  else die "no prompt given"; fi
}

# Run one turn: $1 name, $2 prompt file.
run_turn() {
  local name="$1" pfile="$2" d cwd mode model conv turn out
  d="$(sdir "$name")"
  cwd=$(meta_get "$name" cwd); mode=$(meta_get "$name" mode)
  model=$(meta_get "$name" model); conv=$(meta_get "$name" conversation)
  turn=$(( $(meta_get "$name" turns) + 1 ))
  out="$d/turn-$turn.jsonl"
  cp "$pfile" "$d/turn-$turn.prompt.md"

  local args=(--output-format stream-json)
  case "$mode" in
    edit) args+=(--mode accept-edits) ;;
    plan) args+=(--mode plan) ;;
    yolo) args+=(--mode accept-edits --dangerously-skip-permissions) ;;
  esac
  [ -n "$model" ] && args+=(--model "$model")
  [ -n "$conv" ] && args+=(--conversation "$conv")

  meta_set "$name" turns "$turn"
  meta_set "$name" status running
  meta_set "$name" updated "$(date '+%Y-%m-%d %H:%M:%S')"

  ( cd "$cwd" && exec "$AGY" "${args[@]}" -p "$(cat "$pfile")" < /dev/null ) > "$out" 2> "$d/turn-$turn.stderr" &
  echo $! > "$d/pid"
  wait $! ; local rc=$?
  rm -f "$d/pid"

  [ -z "$conv" ] && conv=$(grep -m1 -o '"conversation_id":"[^"]*"' "$out" | cut -d'"' -f4) \
    && [ -n "$conv" ] && meta_set "$name" conversation "$conv"
  if [ "$(meta_get "$name" status)" = stopped ]; then
    echo "gem: session '$name' turn $turn was stopped"; return 1
  fi
  if grep -q '"event":"result".*"status":"SUCCESS"' "$out"
  then meta_set "$name" status done
  else meta_set "$name" status error; fi
  meta_set "$name" updated "$(date '+%Y-%m-%d %H:%M:%S')"

  echo "=== agy session '$name' turn $turn ($(meta_get "$name" status), exit $rc) ==="
  print_result "$name" "$turn"
}

print_result() {
  local name="$1" turn="$2" d; d="$(sdir "$name")"
  [ -f "$d/turn-$turn.jsonl" ] || die "no turn $turn for '$name'"
  python3 - "$d/turn-$turn.jsonl" "$d/turn-$turn.stderr" "$(meta_get "$name" cwd)" <<'PY'
import json, os, sys
text, result, tools, failed, edited = [], None, set(), [], set()
for line in open(sys.argv[1]):
    try: e = json.loads(line)
    except Exception: continue
    if e.get("event") == "result": result = e.get("result", {}); continue
    s = e.get("step_update") or {}
    if s.get("step_type") == "agent_response": text.append(s.get("text_delta", ""))
    elif s.get("step_type") == "tool":
        tools.add(s.get("step_index"))
        p = (s.get("tool_info") or {}).get("parameters") or {}
        if s.get("state") == "ERROR":
            err = (s.get("tool_info") or {}).get("error") or {}
            failed.append(f"{s.get('tool_name')}: {err.get('message', '').splitlines()[0] if err else ''}")
        elif s.get("tool_name") in ("write_file", "edit_file") and p.get("Path"):
            edited.add(os.path.relpath(p["Path"], sys.argv[3]))
result = result or {}
print((result.get("response") or "".join(text)).strip() or "(no reply)")
if result.get("status") != "SUCCESS":
    print("\nERROR:", result.get("error") or "turn ended without a result")
    tail = [l for l in open(sys.argv[2]).read().splitlines() if l.strip()]
    if tail: print("--- stderr ---\n" + "\n".join(tail[-15:]))
for f in failed: print("tool failed:", f)
for a in result.get("denied_actions", []):
    print(f"denied: {a.get('display_name', a.get('action'))} (not allowed in this mode)")
print(f"\n[tools: {len(tools)} calls, {len(failed)} failed" +
      (f"; edited: {', '.join(sorted(edited))}" if edited else "") + "]")
PY
}

cmd="${1:-}"; shift || true
case "$cmd" in
  start)
    name="${1:-}"; shift || die "usage: gem start <name> ..."
    [[ "$name" =~ ^[A-Za-z0-9._-]+$ ]] || die "name must be [A-Za-z0-9._-]"
    [ -e "$(sdir "$name")" ] && die "session '$name' already exists (use send, or close it)"
    cwd="$PWD"; mode=edit; model="${GEM_MODEL:-}"
    while [ $# -gt 0 ]; do
      case "$1" in
        --cwd) cwd="$(cd "$2" && pwd)" || die "bad --cwd"; shift 2 ;;
        --mode) mode="$2"; shift 2 ;;
        --model) model="$2"; shift 2 ;;
        *) break ;;
      esac
    done
    [[ "$mode" =~ ^(edit|plan|yolo)$ ]] || die "mode must be edit, plan or yolo"
    d="$(sdir "$name")"; mkdir -p "$d"
    printf 'cwd=%s\nmode=%s\nmodel=%s\nturns=0\nstatus=new\ncreated=%s\n' \
      "$cwd" "$mode" "$model" "$(date '+%Y-%m-%d %H:%M:%S')" > "$d/meta"
    p="$d/.prompt"
    { [ -f "$PREAMBLE_FILE" ] && cat "$PREAMBLE_FILE" && printf '\n\n# Task\n\n'; read_prompt "$@"; } > "$p"
    run_turn "$name" "$p" ;;
  send)
    name="${1:-}"; shift || die "usage: gem send <name> PROMPT"
    need_session "$name"
    is_running "$name" && die "session '$name' is still running a turn"
    [ -n "$(meta_get "$name" conversation)" ] || die "session '$name' has no conversation to continue"
    p="$(sdir "$name")/.prompt"; read_prompt "$@" > "$p"
    run_turn "$name" "$p" ;;
  watch)
    [ -n "${1:-}" ] && need_session "$1"
    exec python3 "$SCRIPTS/gem-watch.py" "$ROOT" "${1:-}" ;;
  result)
    name="${1:-}"; need_session "$name"
    print_result "$name" "${2:-$(meta_get "$name" turns)}" ;;
  status)
    name="${1:-}"; need_session "$name"
    s=$(meta_get "$name" status)
    [ "$s" = running ] && ! is_running "$name" && s="dead"
    echo "$s (turn $(meta_get "$name" turns), $(meta_get "$name" mode), $(meta_get "$name" model), $(meta_get "$name" cwd))" ;;
  list)
    printf '%-20s %-8s %-5s %-5s %-24s %-19s %s\n' NAME STATUS TURNS MODE MODEL UPDATED CWD
    for d in "$ROOT"/*/; do
      [ -f "$d/meta" ] || continue; n=$(basename "$d")
      s=$(meta_get "$n" status); [ "$s" = running ] && ! is_running "$n" && s=dead
      m=$(meta_get "$n" model)
      printf '%-20s %-8s %-5s %-5s %-24s %-19s %s\n' "$n" "$s" "$(meta_get "$n" turns)" \
        "$(meta_get "$n" mode)" "${m:-default}" "$(meta_get "$n" updated)" "$(meta_get "$n" cwd)"
    done ;;
  models) "$AGY" models ;;
  stop)
    name="${1:-}"; need_session "$name"
    if is_running "$name"; then
      meta_set "$name" status stopped
      pid=$(cat "$(sdir "$name")/pid"); pkill -TERM -P "$pid" 2>/dev/null; kill -TERM "$pid" 2>/dev/null
      echo "stopped '$name'"
    else echo "'$name' is not running"; fi ;;
  close)
    name="${1:-}"; need_session "$name"
    "$0" stop "$name" >/dev/null
    rm -rf "$(sdir "$name")"; echo "closed '$name'" ;;
  *) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
