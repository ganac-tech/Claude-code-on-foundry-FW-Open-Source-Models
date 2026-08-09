#!/usr/bin/env bash
# Prompt-cache demo — shows input / cached / output tokens for every turn.
#
#   ./demo_cache_direct.sh
#
# At the prompt:
#   /again    re-send the last prompt (the clearest way to see a cache hit)
#   /new      clear the conversation, keep the cache key
#   /newkey   rotate the cache key — usually turns the next hit into a miss
#   /stats    session totals and what the cache saved
#   exit      quit
#
# ── why this talks to Foundry directly, not through the gateway ──────────────
# Envoy AI Gateway v1.0.0 does not carry the upstream OpenAI field
# `usage.prompt_tokens_details.cached_tokens` into the Anthropic response it
# returns — every request comes back reporting cache_read_input_tokens: 0 even
# when Foundry served it entirely from cache. Measured on this deployment: an
# identical prefix reports 0 through the gateway and 5,613 queried directly.
#
# So a cache demo has to use the OpenAI endpoint. Same Foundry deployment, same
# model, same .env — only the path differs. ./demo.sh is the gateway demo.
#
# ── session affinity ────────────────────────────────────────────────────────
# Every request carries a `prompt_cache_key`, generated once per session.
#
# The key is a ROUTING hint, not a hard cache partition. Same key means your
# requests keep landing where your prefix is already cached, which is why a
# repeat is reliably a hit. A different key means you may land elsewhere — a
# miss only if that replica has not seen the content. Measured on this
# deployment: with a never-before-sent prefix, a new key misses every time
# (0%); with a prefix that has been in heavy use, a new key still hit 100%,
# because enough replicas already had it.
set -uo pipefail

cd "$(dirname "$0")"

[[ -f .env ]] || { echo "error: .env not found. Copy .env.example to .env." >&2; exit 1; }
set -a; . ./.env; set +a
: "${FOUNDRY_HOST:?set FOUNDRY_HOST in .env}"
: "${AZURE_API_KEY:?set AZURE_API_KEY in .env}"
: "${FOUNDRY_MODEL:=FW-GLM-5.2}"

HOST="${FOUNDRY_HOST#*://}"; HOST="${HOST%%/*}"; HOST="${HOST%%:*}"
URL="https://${HOST}/openai/v1/chat/completions"

# Pricing per 1M tokens — GLM 5.2 list. Override in .env if you have a
# negotiated Azure rate.
: "${GLM_PRICE_IN:=1.40}"
: "${GLM_PRICE_CACHED:=0.14}"
: "${GLM_PRICE_OUT:=4.40}"

STATE="$(mktemp -d "${TMPDIR:-/tmp}/demo_cache.XXXXXX")"
trap 'rm -rf "$STATE"' EXIT
HISTORY="$STATE/history.json"
TOTALS="$STATE/totals.json"
echo '[]' > "$HISTORY"
echo '{"turns":0,"in":0,"cached":0,"out":0,"ms":0}' > "$TOTALS"

new_key() { echo "sess-$(python3 -c 'import uuid;print(uuid.uuid4().hex[:12])')"; }
CACHE_KEY_SESSION="$(new_key)"

# A realistic system prompt. Prompt caching only pays off on a substantial
# shared prefix — this stands in for the ~15k tokens of instructions and tool
# definitions a real coding agent sends on every single turn.
cat > "$STATE/system.txt" <<'SYSTEM'
You are a senior backend engineer reviewing and writing production Python.

General approach:
- Prefer the smallest change that solves the stated problem. Do not refactor
  surrounding code, rename variables, or restructure modules unless the task
  asks for it.
- Read the code as written before suggesting anything. If behaviour depends on
  something not shown, say what you would need to see rather than assuming.
- When several approaches are reasonable, pick one, implement it, and note the
  alternative in a sentence. Do not present a menu.

Correctness:
- Integer division, off-by-one boundaries, and empty-collection cases are the
  three places bugs hide most often in this codebase. Check them explicitly.
- Any function that touches money uses Decimal, never float. Rounding is
  ROUND_HALF_UP at two places unless the caller specifies otherwise.
- Timezone-naive datetimes are a bug. Everything is UTC internally and
  converted only at the presentation boundary.
- Mutable default arguments are a bug. Use None and construct inside.

Concurrency:
- Shared mutable state crossing a thread boundary needs an explicit lock or an
  immutable handoff. Point out read-modify-write sequences that are not atomic.
- Retries must be idempotent. If an operation cannot be made idempotent, it
  needs a deduplication key.
- Database transactions acquire locks in a documented order to avoid deadlock.

Errors and validation:
- Validate at system boundaries: user input, external API responses, message
  payloads. Do not re-validate internal calls that the type system covers.
- Catch specific exceptions. A bare except that swallows KeyboardInterrupt or
  SystemExit is a defect.
- Error messages state what failed and what the caller should do about it. No
  apologies, no internal identifiers the caller cannot act on.

Style:
- Match the surrounding file's conventions over any global preference.
- Comments explain constraints the code cannot express. Do not comment what the
  next line does or narrate the change you made.
- Type hints on public functions. Internal helpers only where inference fails.

Testing:
- A bug fix comes with a test that fails before the fix and passes after.
- Test the boundary, not the middle: empty, one, many, and the exact threshold.
- Do not mock what you own. Mock the network and the clock, nothing else.

Answer format:
- Lead with the answer or the code. Explanation after, only if it changes what
  the reader would do.
- Code blocks are complete and runnable, not fragments with ellipses.
SYSTEM

PY="$STATE/turn.py"
cat > "$PY" <<'PYEOF'
import json, sys, time, urllib.request, urllib.error
from pathlib import Path

url, api_key, model, cache_key, prompt, hist_p, sys_p, tot_p = sys.argv[1:9]
history = json.loads(Path(hist_p).read_text())
system = Path(sys_p).read_text()

messages = [{"role": "system", "content": system}] + history + \
           [{"role": "user", "content": prompt}]
body = json.dumps({
    "model": model, "max_tokens": 1024,
    "prompt_cache_key": cache_key,      # session affinity
    "messages": messages,
}).encode()

t0 = time.time()
try:
    req = urllib.request.Request(url, data=body,
        headers={"api-key": api_key, "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read())
except urllib.error.HTTPError as e:
    detail = e.read()[:300].decode("utf-8", "replace")
    print(f"\x1b[31m  request failed: HTTP {e.code}\x1b[0m")
    print(f"\x1b[2m  {detail}\x1b[0m")
    sys.exit(1)
except Exception as e:
    print(f"\x1b[31m  request failed: {type(e).__name__}: {e}\x1b[0m")
    sys.exit(1)
ms = int((time.time() - t0) * 1000)

if "error" in d:
    print(f"\x1b[31m  {str(d['error'])[:300]}\x1b[0m")
    sys.exit(1)

msg = d["choices"][0]["message"]
text = (msg.get("content") or "").strip()
u = d.get("usage", {})
tin = u.get("prompt_tokens", 0)
tout = u.get("completion_tokens", 0)
cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
pct = (100.0 * cached / tin) if tin else 0.0

for line in (text or "(empty response)").splitlines():
    print("   " + line)

badge = "\x1b[32mCACHE HIT\x1b[0m" if pct >= 50 else (
        "\x1b[33mpartial\x1b[0m" if pct > 0 else "\x1b[2mcold\x1b[0m")
print(f"\x1b[2m   ─ in {tin:,}  ·  cached {cached:,} ({pct:.0f}%)  ·  "
      f"out {tout:,}  ·  {ms:,}ms  ─ \x1b[0m{badge}")

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
actual = uncached/1e6*p_in + t["cached"]/1e6*p_cached + t["out"]/1e6*p_out
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

bold "Prompt cache demo  ·  ${FOUNDRY_MODEL}  ·  ${HOST}"
dim  "Direct to Foundry — the gateway does not report cached tokens (see header)."
dim  "cache key: ${CACHE_KEY_SESSION}"
dim  "/again  /new  /newkey  /stats  exit"
hr

LAST=""
while true; do
  printf '\n\033[1;36myou>\033[0m '
  IFS= read -r line || { echo; break; }

  case "$line" in
    ""|" ") continue ;;
    exit|quit|/exit|/quit) break ;;
    /stats) echo; show_stats; continue ;;
    /new)
      echo '[]' > "$HISTORY"
      dim "   conversation cleared — cache key unchanged, so re-asking your"
      dim "   first question should now be a full cache hit"
      continue ;;
    /newkey)
      CACHE_KEY_SESSION="$(new_key)"
      echo '[]' > "$HISTORY"
      dim "   new cache key: ${CACHE_KEY_SESSION}"
      dim "   new key — the same prompt will usually miss now, though a"
      dim "   heavily-used prefix may still be cached wherever you land"
      continue ;;
    /again)
      [[ -z $LAST ]] && { dim "   nothing sent yet"; continue; }
      printf '\033[2m   re-sending: %s\033[0m\n' "$LAST"
      line="$LAST" ;;
    /help)
      dim "   /again   re-send the last prompt"
      dim "   /new     clear conversation, keep cache key"
      dim "   /newkey  rotate cache key (forces a miss)"
      dim "   /stats   session totals"
      continue ;;
  esac

  echo
  python3 "$PY" "$URL" "$AZURE_API_KEY" "$FOUNDRY_MODEL" "$CACHE_KEY_SESSION" \
          "$line" "$HISTORY" "$STATE/system.txt" "$TOTALS"
  LAST="$line"
done

echo
hr
show_stats
