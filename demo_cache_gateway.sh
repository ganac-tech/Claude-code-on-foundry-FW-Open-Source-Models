#!/usr/bin/env bash
# Prompt-cache demo, with the traffic going THROUGH the gateway.
#
#   ./start-gateway.sh          # terminal 1
#   ./demo_cache_gateway.sh     # terminal 2
#
# Every prompt is sent to the gateway as an Anthropic request, translated to
# OpenAI, and answered by GLM 5.2 in Foundry. The caching happens in the model.
#
# ── how the cached number is obtained ───────────────────────────────────────
# Envoy AI Gateway v1.0.0 does not carry the upstream OpenAI field
# `usage.prompt_tokens_details.cached_tokens` into the Anthropic response, so
# asking the gateway how much was cached always returns 0 — even on a request
# GLM served entirely from cache.
#
# So this script reads the meter separately. Before each turn it sends a
# one-token probe straight to Foundry with the same message prefix and the same
# `prompt_cache_key` the gateway injects, and reads `cached_tokens` off that.
# The probe reports the cache state as it was *before* the turn — which is
# exactly what the gateway's request is about to find.
#
#   probe (1 token, direct)  ──▶ reads the cache meter
#   your prompt              ──▶ gateway ──▶ Foundry ──▶ GLM 5.2   (the real turn)
#
# input and output tokens come from the gateway's own response; only the cached
# figure comes from the probe. ./demo_cache_direct.sh is the direct-only version.
#
# ── session affinity ────────────────────────────────────────────────────────
# The gateway pins `prompt_cache_key` to CACHE_KEY from .env, via bodyMutation
# in aigw-foundry.yaml. That is deliberate and fleet-wide: every Claude Code
# user sends the same large system prompt, so one shared key lets them all reuse
# one cached copy. A per-user key would split it and cost more.
#
# It also means a client cannot choose its own key here — the gateway overwrites
# whatever arrives. To route on a different key, change CACHE_KEY in .env and
# restart the gateway. `/key` shows the one in force.
#
# Note the key is a routing hint, not a hard partition: changing it does not
# reliably produce a cold start. A prefix already cached on many replicas can
# still hit under a brand-new key.
set -uo pipefail

cd "$(dirname "$0")"

[[ -f .env ]] || { echo "error: .env not found. Copy .env.example to .env." >&2; exit 1; }
set -a; . ./.env; set +a
: "${FOUNDRY_HOST:?set FOUNDRY_HOST in .env}"
: "${AZURE_API_KEY:?set AZURE_API_KEY in .env}"
: "${FOUNDRY_MODEL:=FW-GLM-5.2}"
: "${CACHE_KEY:=claude-code-fleet}"
: "${GLM_PRICE_IN:=1.40}"
: "${GLM_PRICE_CACHED:=0.14}"
: "${GLM_PRICE_OUT:=4.40}"

GW="${GW:-http://localhost:1975/anthropic}"
HOST="${FOUNDRY_HOST#*://}"; HOST="${HOST%%/*}"; HOST="${HOST%%:*}"
PROBE_URL="https://${HOST}/openai/v1/chat/completions"

if ! curl -sf --max-time 3 http://localhost:1064/health >/dev/null 2>&1; then
  echo "Gateway is not running. Start it first:  ./start-gateway.sh" >&2
  exit 1
fi

STATE="$(mktemp -d "${TMPDIR:-/tmp}/demo_cache_gw.XXXXXX")"
trap 'rm -rf "$STATE"' EXIT
HISTORY="$STATE/history.json"; echo '[]' > "$HISTORY"
TOTALS="$STATE/totals.json"; echo '{"turns":0,"in":0,"cached":0,"out":0,"ms":0}' > "$TOTALS"

# Stands in for the ~15k tokens of instructions a real coding agent sends every
# turn — caching only pays off on a substantial shared prefix.
cat > "$STATE/system.txt" <<'SYSTEM'
You are a senior backend engineer reviewing and writing production Python.

General approach:
- Prefer the smallest change that solves the stated problem. Do not refactor
  surrounding code, rename variables, or restructure modules unless asked.
- Read the code as written before suggesting anything. If behaviour depends on
  something not shown, say what you would need to see rather than assuming.
- When several approaches are reasonable, pick one and implement it. Note the
  alternative in a sentence. Do not present a menu.

Correctness:
- Integer division, off-by-one boundaries, and empty-collection cases are where
  bugs hide most often here. Check them explicitly.
- Anything touching money uses Decimal, never float, rounding ROUND_HALF_UP at
  two places unless the caller specifies otherwise.
- Timezone-naive datetimes are a bug. Everything is UTC internally, converted
  only at the presentation boundary.
- Mutable default arguments are a bug. Use None and construct inside.

Concurrency:
- Shared mutable state crossing a thread boundary needs an explicit lock or an
  immutable handoff. Flag read-modify-write sequences that are not atomic.
- Retries must be idempotent. If an operation cannot be, it needs a dedup key.
- Database transactions acquire locks in a documented order to avoid deadlock.

Errors and validation:
- Validate at system boundaries: user input, external API responses, message
  payloads. Do not re-validate internal calls the type system already covers.
- Catch specific exceptions. A bare except that swallows KeyboardInterrupt or
  SystemExit is a defect.
- Error messages state what failed and what the caller should do about it.

Style:
- Match the surrounding file's conventions over any global preference.
- Comments explain constraints the code cannot express. Do not narrate what the
  next line does or describe the change you just made.
- Type hints on public functions; internal helpers only where inference fails.

Testing:
- A bug fix comes with a test that fails before it and passes after.
- Test the boundary, not the middle: empty, one, many, and the exact threshold.
- Do not mock what you own. Mock the network and the clock, nothing else.

Answer format:
- Lead with the answer or the code. Explanation after, and only if it changes
  what the reader would do.
- Code blocks are complete and runnable, not fragments with ellipses.
SYSTEM

PY="$STATE/turn.py"
cat > "$PY" <<'PYEOF'
import json, sys, time, urllib.request, urllib.error
from pathlib import Path

(gw, probe_url, api_key, model, cache_key, prompt,
 hist_p, sys_p, tot_p) = sys.argv[1:10]

history = json.loads(Path(hist_p).read_text())
system  = Path(sys_p).read_text()

def post(url, payload, headers, timeout=180):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

# ── 1. read the cache meter, before the real turn warms it ──────────────────
# Same prefix and same key the gateway will use, one output token. Its
# cached_tokens is the state the gateway's request is about to encounter.
cached = None
try:
    d = post(probe_url, {
        "model": model, "max_tokens": 1, "prompt_cache_key": cache_key,
        "messages": [{"role": "system", "content": system}] + history +
                    [{"role": "user", "content": prompt}],
    }, {"api-key": api_key, "content-type": "application/json"}, timeout=120)
    pu = d.get("usage", {})
    cached = (pu.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
except Exception:
    pass    # probe is best-effort; the turn still runs

# ── 2. the real turn, through the gateway ───────────────────────────────────
t0 = time.time()
try:
    d = post(f"{gw}/v1/messages", {
        "model": "claude-opus-5",          # rewritten by the gateway
        "max_tokens": 1024,
        "system": system,
        "messages": history + [{"role": "user", "content": prompt}],
    }, {"content-type": "application/json", "anthropic-version": "2023-06-01"})
except urllib.error.HTTPError as e:
    print(f"\x1b[31m  gateway returned HTTP {e.code}\x1b[0m")
    print(f"\x1b[2m  {e.read()[:300].decode('utf-8','replace')}\x1b[0m")
    sys.exit(1)
except Exception as e:
    print(f"\x1b[31m  gateway request failed: {type(e).__name__}: {e}\x1b[0m")
    sys.exit(1)
ms = int((time.time() - t0) * 1000)

if d.get("type") == "error":
    print(f"\x1b[31m  {str(d.get('error'))[:300]}\x1b[0m")
    sys.exit(1)

text = "".join(b.get("text", "") for b in d.get("content", [])
               if b.get("type") == "text").strip()
u    = d.get("usage", {})
tin  = u.get("input_tokens", 0)
tout = u.get("output_tokens", 0)

for line in (text or "(empty response)").splitlines():
    print("   " + line)

if cached is None:
    meter = "\x1b[2m   ─ in {:,}  ·  cached ?  ·  out {:,}  ·  {:,}ms ─\x1b[0m".format(tin, tout, ms)
    print(meter + " \x1b[33mprobe failed\x1b[0m")
    cached = 0
else:
    pct = (100.0 * cached / tin) if tin else 0.0
    badge = "\x1b[32mCACHE HIT\x1b[0m" if pct >= 50 else (
            "\x1b[33mpartial\x1b[0m" if pct > 0 else "\x1b[2mcold\x1b[0m")
    print(f"\x1b[2m   ─ in {tin:,}  ·  cached {cached:,} ({pct:.0f}%)  ·  "
          f"out {tout:,}  ·  {ms:,}ms ─\x1b[0m {badge}")
    print(f"\x1b[2m     in/out via gateway · cached via probe · served by "
          f"{d.get('model','?')}\x1b[0m")

history += [{"role": "user", "content": prompt},
            {"role": "assistant", "content": text}]
Path(hist_p).write_text(json.dumps(history))

t = json.loads(Path(tot_p).read_text())
t["turns"] += 1; t["in"] += tin; t["cached"] += cached
t["out"] += tout; t["ms"] += ms
Path(tot_p).write_text(json.dumps(t))
PYEOF

hr()   { printf '\033[2m%s\033[0m\n' "$(printf '%.0s─' {1..72})"; }
dim()  { printf '\033[2m%s\033[0m\n' "$1"; }
bold() { printf '\033[1m%s\033[0m\n' "$1"; }

show_stats() {
  python3 - "$TOTALS" "$GLM_PRICE_IN" "$GLM_PRICE_CACHED" "$GLM_PRICE_OUT" <<'EOF'
import json, sys
t = json.load(open(sys.argv[1]))
p_in, p_cached, p_out = map(float, sys.argv[2:5])
if not t["turns"]:
    print("   no turns yet"); raise SystemExit
uncached = max(0, t["in"] - t["cached"])
actual  = uncached/1e6*p_in + t["cached"]/1e6*p_cached + t["out"]/1e6*p_out
nocache = t["in"]/1e6*p_in + t["out"]/1e6*p_out
hit = 100.0*t["cached"]/t["in"] if t["in"] else 0
print(f"   turns          {t['turns']}")
print(f"   input tokens   {t['in']:,}   of which cached {t['cached']:,} ({hit:.0f}%)")
print(f"   output tokens  {t['out']:,}")
print(f"   avg latency    {t['ms']//t['turns']:,}ms")
print(f"   cost           ${actual:.5f}")
print(f"   without cache  ${nocache:.5f}"
      + (f"   ({100*(1-actual/nocache):.0f}% saved)" if nocache else ""))
EOF
}

bold "Prompt cache demo · through the gateway"
dim  "Claude Code format → localhost:1975 → Foundry → ${FOUNDRY_MODEL} (caches)"
dim  "cache key in force: ${CACHE_KEY}   (set by the gateway, fleet-wide)"
dim  "/again  /new  /key  /stats  exit"
hr

LAST=""
while true; do
  printf '\n\033[1;36myou>\033[0m '
  IFS= read -r line || { echo; break; }

  case "$line" in
    ""|" ") continue ;;
    exit|quit|/exit|/quit) break ;;
    /stats) echo; show_stats; continue ;;
    /key)
      dim "   ${CACHE_KEY}"
      dim "   Set by bodyMutation in aigw-foundry.yaml, from CACHE_KEY in .env."
      dim "   To route on a different key: change it there and restart the gateway."
      continue ;;
    /new)
      echo '[]' > "$HISTORY"
      dim "   conversation cleared — the cached system prompt survives, so"
      dim "   re-asking your first question should now be a cache hit"
      continue ;;
    /again)
      [[ -z $LAST ]] && { dim "   nothing sent yet"; continue; }
      printf '\033[2m   re-sending: %s\033[0m\n' "$LAST"
      line="$LAST" ;;
    /help)
      dim "   /again   re-send the last prompt"
      dim "   /new     clear conversation, keep the cached system prompt"
      dim "   /key     show the cache key the gateway is injecting"
      dim "   /stats   session totals"
      continue ;;
  esac

  echo
  python3 "$PY" "$GW" "$PROBE_URL" "$AZURE_API_KEY" "$FOUNDRY_MODEL" \
          "$CACHE_KEY" "$line" "$HISTORY" "$STATE/system.txt" "$TOTALS"
  LAST="$line"
done

echo
hr
show_stats
