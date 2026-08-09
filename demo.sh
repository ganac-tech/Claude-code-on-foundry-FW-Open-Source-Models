#!/usr/bin/env bash
# Interactive demo: ask GLM 5.2 on Foundry anything, through unmodified Claude Code.
#
# After each answer it prints the gateway's own live counters — the proof the
# request was served by GLM on Foundry and never touched api.anthropic.com.
#
#   ./start-gateway.sh          # terminal 1, leave running
#   ./demo.sh                   # terminal 2 — interactive prompt
#   ./demo.sh --canned          # three scripted questions, no typing
#   ./demo.sh "one question"    # one-shot
#
# At the prompt:  /new  start a fresh conversation      /log  full session log
#                 /help show commands                   exit  quit
set -uo pipefail

cd "$(dirname "$0")"

if ! curl -sf --max-time 3 http://localhost:1064/health >/dev/null 2>&1; then
  echo "Gateway is not running. Start it first:  ./start-gateway.sh" >&2
  exit 1
fi

# The JSON access log is written to Envoy's own stdout inside the current run
# directory — NOT to the aigw process stdout you see in terminal 1.
GWLOG="$(ls -td "${AIGW_STATE_HOME:-$HOME/.local/state/aigw}"/envoy-runs/*/ 2>/dev/null \
         | head -1)stdout.log"
[[ -f $GWLOG ]] || GWLOG=/dev/null

[[ -f .env ]] && { set -a; . ./.env; set +a; }
MODEL="${FOUNDRY_MODEL:-FW-GLM-5.2}"

# Claude Code doesn't know this deployment's context window and warns about it,
# then assumes a conservative 200k. Set GLM_CONTEXT_TOKENS in .env to silence it.
[[ -n ${GLM_CONTEXT_TOKENS:-} ]] && export CLAUDE_CODE_MAX_CONTEXT_TOKENS="$GLM_CONTEXT_TOKENS"

export ANTHROPIC_BASE_URL="http://localhost:1975/anthropic"
export ANTHROPIC_AUTH_TOKEN="gateway-injected"
export ANTHROPIC_MODEL="$MODEL"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$MODEL"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$MODEL"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$MODEL"
export ANTHROPIC_DEFAULT_FABLE_MODEL="$MODEL"
export CLAUDE_CODE_SUBAGENT_MODEL="$MODEL"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

hr()   { printf '%.0s─' {1..70}; echo; }
dim()  { printf '\033[2m%s\033[0m\n' "$1"; }
bold() { printf '\033[1m%s\033[0m\n' "$1"; }

# Two known-cosmetic warnings, filtered so they don't drown the demo:
#  - "connectors are disabled": we set ANTHROPIC_AUTH_TOKEN, which outranks the
#    claude.ai login. Expected and harmless.
#  - "not a model this version of Claude Code recognizes": Claude Code has no
#    entry for the Foundry deployment name, so it assumes a 200k window.
# Everything else from stderr still comes through.
strip_known_warnings() {
  grep -vE 'connectors are disabled|is not a model this version of Claude Code recognizes'
}

# Snapshot the gateway's live counters: "<requests> <input_tokens> <output_tokens> <served_by>".
#
# Read from the aigw metrics endpoint, NOT the Envoy access log. Envoy buffers
# that log and flushes on a ~10s timer, so an access-log-based proof shows up
# two questions late in an interactive session. The metrics update instantly.
gw_snapshot() {
  curl -s --max-time 5 http://localhost:1064/metrics 2>/dev/null | python3 -c "
import sys, re
reqs = 0.0; toks = {}; served = set()
for line in sys.stdin:
    m = re.match(r'gen_ai_server_request_duration_seconds_count\{(.*)\} ([0-9.e+]+)', line)
    if m:
        reqs += float(m.group(2))
        r = re.search(r'gen_ai_response_model=\"([^\"]+)\"', m.group(1))
        if r: served.add(r.group(1))
    m = re.match(r'gen_ai_client_token_usage_sum\{(.*)\} ([0-9.e+]+)', line)
    if m:
        t = re.search(r'gen_ai_token_type=\"([a-z_]+)\"', m.group(1))
        if t: toks[t.group(1)] = toks.get(t.group(1), 0.0) + float(m.group(2))
print(int(reqs), int(toks.get('input', 0)), int(toks.get('output', 0)),
      ','.join(sorted(served)) or '?')
" 2>/dev/null || echo "0 0 0 ?"
}

# Print what changed at the gateway since snapshot $1 — the provenance proof.
show_proof() {
  local before=$1
  local after; after=$(gw_snapshot)
  read -r r0 i0 o0 _        <<<"$before"
  read -r r1 i1 o1 served   <<<"$after"
  printf '   \033[2m[gateway] served_by=%s  requests=+%s  in=+%s out=+%s\033[0m\n' \
    "$served" "$((r1 - r0))" "$((i1 - i0))" "$((o1 - o0))"
}

# Run one question. $1 = question, $2 = "continue" to keep conversation context.
ask() {
  local q=$1 cont=${2:-}
  local baseline
  baseline=$(gw_snapshot)

  # < /dev/null is load-bearing: `claude -p` reads stdin as extra input, so
  # without it the REPL's own stdin gets swallowed and the next question(s)
  # disappear into this call instead of being prompted for.
  if [[ $cont == continue ]]; then
    claude -p --continue "$q" </dev/null 2>&1 | strip_known_warnings | sed 's/^/   /'
  else
    claude -p "$q" </dev/null 2>&1 | strip_known_warnings | sed 's/^/   /'
  fi

  show_proof "$baseline"
}

bold "Claude Code  ->  localhost:1975/anthropic  ->  Foundry  ->  $MODEL"

# ---- one-shot / canned modes -------------------------------------------------
if [[ ${1:-} == --canned ]]; then
  hr
  first=1
  for q in "What is 17 * 23? Reply with just the number." \
           "Write a Python function that reverses a linked list. Code only, no explanation." \
           "Name three tradeoffs of optimistic vs pessimistic locking. One line each."; do
    echo; bold "Q: $q"; echo
    [[ $first == 1 ]] && ask "$q" || ask "$q" continue
    first=0
    hr
  done
  exit 0
fi

if [[ $# -gt 0 ]]; then
  hr
  first=1
  for q in "$@"; do
    echo; bold "Q: $q"; echo
    [[ $first == 1 ]] && ask "$q" || ask "$q" continue
    first=0
    hr
  done
  exit 0
fi

# ---- interactive -------------------------------------------------------------
dim "Type a question and press enter. Follow-ups keep context."
dim "/new resets the conversation · /log opens the full log · exit quits"
hr

turn=0
while true; do
  printf '\n\033[1;36myou>\033[0m '
  if ! IFS= read -r line; then echo; break; fi

  case "$line" in
    ""|" ") continue ;;
    exit|quit|/exit|/quit) break ;;
    /help)
      dim "  /new   start a fresh conversation (drops prior context)"
      dim "  /log   show every gateway request this session"
      dim "  exit   quit"
      continue ;;
    /new)
      turn=0
      dim "  context cleared — next question starts a new conversation"
      continue ;;
    /log)
      echo
      dim "  (Envoy flushes this log every ~10s, so the last request may be missing)"
      tail -40 "$GWLOG" 2>/dev/null | grep -o '{.*}' | python3 -c "
import json, sys
rows=[]
for l in sys.stdin:
    try: rows.append(json.loads(l))
    except Exception: pass
if not rows: print('   (nothing logged yet)')
else:
    w = max([len(str(d.get('gen_ai.response.model',''))) for d in rows] + [9])
    print(f\"   {'served_by':<{w}} {'http':<5} {'in':>8} {'out':>6} {'ms':>7}\")
    for d in rows:
        print(f\"   {str(d.get('gen_ai.response.model','?')):<{w}}\"
              f\" {str(d.get('response_code','?')):<5}\"
              f\" {str(d.get('gen_ai.usage.input_tokens','?')):>8}\"
              f\" {str(d.get('gen_ai.usage.output_tokens','?')):>6}\"
              f\" {str(d.get('duration_ms','?')):>7}\")
"
      continue ;;
  esac

  echo
  [[ $turn == 0 ]] && ask "$line" || ask "$line" continue
  turn=$((turn + 1))
done

echo
dim "Every request above left this machine as POST /openai/v1/chat/completions"
dim "to your Foundry endpoint. Claude Code never contacted api.anthropic.com."
