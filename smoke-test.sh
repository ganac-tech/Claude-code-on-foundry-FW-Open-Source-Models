#!/usr/bin/env bash
# Verify the gateway translates Anthropic -> OpenAI -> Foundry correctly.
# Run this BEFORE pointing Claude Code at it — it isolates gateway problems
# from Claude Code problems.
#
# Start the gateway in another terminal first: ./start-gateway.sh
set -uo pipefail

cd "$(dirname "$0")"
# NOTE: the gateway serves the Anthropic Messages API under /anthropic, not at the
# root. The extproc registers its processor at the exact path /anthropic/v1/messages;
# POSTing to /v1/messages returns 404 "unsupported path". This is why Claude Code's
# ANTHROPIC_BASE_URL must end in /anthropic.
GW="${GW:-http://localhost:1975/anthropic}"
pass=0; fail=0

# max_tokens is deliberately generous. GLM 5.2 is a reasoning model and spends
# tokens thinking before it emits any visible content — at max_tokens=64 the
# whole budget goes to reasoning, the response carries no content block at all,
# and the stream ends with stop_reason=max_tokens after message_start. That
# looks like a broken gateway but is just the budget being too small.

check() { # name, condition-result
  if [[ "$2" == "0" ]]; then echo "  PASS  $1"; pass=$((pass+1))
  else echo "  FAIL  $1"; fail=$((fail+1)); fi
}

echo "== 0. gateway health =="
curl -sf --max-time 5 http://localhost:1064/health >/dev/null 2>&1
check "admin /health reachable" "$?"
if [[ $fail -gt 0 ]]; then
  echo; echo "Gateway is not up. Run ./start-gateway.sh in another terminal." >&2
  exit 1
fi

# The admin port goes green before Envoy finishes loading the :1975 listener.
# Without this the first real request can come back empty and every assertion
# below fails for the wrong reason.
for _ in $(seq 30); do
  [[ -n "$(curl -s --max-time 5 "$GW/v1/messages" -H 'content-type: application/json' \
      -d '{"model":"probe","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}' 2>/dev/null)" ]] && break
  sleep 1
done
check "data plane on :1975 answering" "$?"

echo
echo "== 1. non-streaming /v1/messages =="
resp=$(curl -s --max-time 120 "$GW/v1/messages" \
  -H 'content-type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{
    "model": "claude-opus-5",
    "max_tokens": 2048,
    "messages": [{"role": "user", "content": "Reply with exactly the word: pong"}]
  }')

echo "$resp" | python3 -c "import json,sys; json.load(sys.stdin)" 2>/dev/null
check "response is valid JSON" "$?"

echo "$resp" | python3 -c "
import json,sys
d=json.load(sys.stdin)
assert d.get('type')=='message', f\"type={d.get('type')}\"
assert d.get('role')=='assistant'
assert isinstance(d.get('content'), list) and d['content']
" 2>/dev/null
check "Anthropic Message envelope (type/role/content[])" "$?"

echo "$resp" | python3 -c "
import json,sys
d=json.load(sys.stdin)
u=d['usage']
assert u['input_tokens']>0 and u['output_tokens']>0, u
" 2>/dev/null
check "usage.input_tokens / output_tokens populated" "$?"

echo "  ---- model echoed back: $(echo "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("model","?"))' 2>/dev/null)"
echo "  ---- text: $(echo "$resp" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("".join(b.get("text","") for b in d.get("content",[]) if b.get("type")=="text")[:120])' 2>/dev/null || echo "$resp" | head -c 300)"

echo
echo "== 2. streaming SSE =="
sse=$(curl -sN --max-time 120 "$GW/v1/messages" \
  -H 'content-type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{
    "model": "claude-opus-5",
    "max_tokens": 2048,
    "stream": true,
    "messages": [{"role": "user", "content": "Count: one two three"}]
  }')

for ev in message_start content_block_delta message_delta message_stop; do
  grep -q "event: $ev" <<<"$sse"
  check "SSE emits $ev" "$?"
done

echo
echo "== 3. tool_use round-trip =="
tool=$(curl -s --max-time 120 "$GW/v1/messages" \
  -H 'content-type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{
    "model": "claude-opus-5",
    "max_tokens": 2048,
    "tool_choice": {"type": "any"},
    "tools": [{
      "name": "get_weather",
      "description": "Get the current weather for a city.",
      "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "City name"}},
        "required": ["city"]
      }
    }],
    "messages": [{"role": "user", "content": "What is the weather in Paris?"}]
  }')

echo "$tool" | python3 -c "
import json,sys
d=json.load(sys.stdin)
blocks=[b for b in d.get('content',[]) if b.get('type')=='tool_use']
assert blocks, f\"no tool_use block; stop_reason={d.get('stop_reason')}\"
b=blocks[0]
assert b['name']=='get_weather', b['name']
assert isinstance(b['input'], dict), b['input']
assert b.get('id'), 'tool_use block missing id'
" 2>/dev/null
check "tool_use block returned in Anthropic shape" "$?"

echo "$tool" | python3 -c "
import json,sys
assert json.load(sys.stdin).get('stop_reason')=='tool_use'
" 2>/dev/null
check "stop_reason == tool_use" "$?"

echo
echo "======================================"
echo "  $pass passed, $fail failed"
echo "======================================"
[[ $fail -eq 0 ]] || {
  echo
  echo "Last raw response for debugging:" >&2
  echo "$tool" | head -c 800 >&2
  echo >&2
  exit 1
}
