#!/usr/bin/env bash
# End-to-end verification of the Anthropic -> OpenAI translation with NO Azure
# credentials and NO network. Starts a mock Foundry, starts the gateway against
# it, runs the smoke test, prints what the gateway sent upstream, tears down.
#
#   ./test/run-offline-test.sh
set -uo pipefail

cd "$(dirname "$0")/.."
MOCK_PORT=8931
pids=()

cleanup() {
  for pid in "${pids[@]:-}"; do kill "$pid" 2>/dev/null; done
  wait 2>/dev/null
}
trap cleanup EXIT

echo "starting mock Foundry on :$MOCK_PORT"
python3 test/mock_foundry.py "$MOCK_PORT" >/tmp/mock-foundry.log 2>&1 &
pids+=($!)
until nc -z localhost "$MOCK_PORT" 2>/dev/null; do sleep 0.5; done

echo "starting gateway"
./bin/aigw run test/aigw-mock.yaml >/tmp/aigw-mock.log 2>&1 &
pids+=($!)

# The admin /health on :1064 goes green BEFORE Envoy finishes loading the :1975
# listener, so waiting on it alone races the first request. Probe the real data
# plane until it actually answers.
echo -n "waiting for data plane"
for _ in $(seq 60); do
  probe=$(curl -s --max-time 5 http://localhost:1975/anthropic/v1/messages \
    -H 'content-type: application/json' \
    -d '{"model":"probe","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}' 2>/dev/null)
  [[ -n "$probe" ]] && break
  echo -n "."
  sleep 1
done
echo " ready"
rm -f test/upstream-requests.jsonl   # discard the probe

echo
./smoke-test.sh
rc=$?

echo
echo "== what the gateway sent upstream =="
python3 -c "
import json
for line in open('test/upstream-requests.jsonl'):
    r = json.loads(line); b = r['body']
    print(f\"  {r['path']}  api-key={r['headers'].get('api-key')}  model={b.get('model')}\")
    if b.get('stream'):
        print(f\"    stream={b['stream']} stream_options={b.get('stream_options')}\")
    if b.get('tools'):
        print(f\"    tool_choice={b.get('tool_choice')} tools[0].function.name=\"
              f\"{b['tools'][0]['function']['name']}\")
" 2>/dev/null || echo "  (no requests recorded)"

exit $rc
