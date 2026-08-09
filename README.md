# Claude Code → Envoy AI Gateway → Foundry (FW-GLM-5.2)

Runs Claude Code against a Fireworks **GLM 5.2** deployment in **Microsoft Foundry**,
with a local Envoy AI Gateway doing the protocol translation.

```
Claude Code ──POST /anthropic/v1/messages──▶  aigw :1975  ──POST /openai/v1/chat/completions──▶  Foundry
   (Anthropic Messages API)                    [translate]              (OpenAI schema)              FW-GLM-5.2
```

## Why a gateway is needed at all

Fireworks *does* ship a native Anthropic `/v1/messages` endpoint — but only on
`api.fireworks.ai`. Fireworks models deployed inside **Foundry are OpenAI
chat-completions only** ([docs](https://docs.fireworks.ai/ecosystem/integrations/azure-foundry)).
Claude Code speaks Anthropic and nothing else. Fireworks' own FireConnect tool
[lists Claude Code as unsupported on the Foundry path](https://docs.fireworks.ai/ecosystem/fireconnect/microsoft-foundry)
— "Claude Code is the remaining gap."

Envoy AI Gateway closes that gap: its `Anthropic` request handler converts
`/v1/messages` into `/v1/chat/completions` and converts the response back,
including streaming SSE, tool calls, and reasoning blocks.

If you ever drop the Foundry requirement (Azure billing / MACC / data residency
are the usual reasons to keep it), point Claude Code straight at
`https://api.fireworks.ai/inference` and delete this whole gateway — Fireworks
serves Anthropic natively there.

## Setup

```bash
cp .env.example .env      # fill in FOUNDRY_HOST and AZURE_API_KEY
./start-gateway.sh        # terminal 1 — gateway on :1975
./smoke-test.sh           # terminal 2 — verify before involving Claude Code
./claude-foundry.sh       # terminal 2 — launch Claude Code against it
```

The `aigw` binary (v1.0.0, darwin-arm64) is already in `bin/`, and its Envoy
runtime is cached in `~/.local/share/aigw`. No Docker, no Kubernetes.

## Running the demo

`demo.sh` is an interactive prompt. Type any question; after each answer it
prints the gateway's live counters — the proof the request was served by GLM
on Foundry and never reached Anthropic.

```bash
./start-gateway.sh                      # terminal 1, leave running
./demo.sh                               # terminal 2 — interactive
```

```
Claude Code  ->  localhost:1975/anthropic  ->  Foundry  ->  FW-GLM-5.2-standard
Type a question and press enter. Follow-ups keep context.
/new resets the conversation · /log opens the full log · exit quits
──────────────────────────────────────────────────────────────────────

you> What is 17 * 23? Reply with just the number.

   391
   [gateway] served_by=accounts/fireworks/models/glm-5p2  requests=+1  in=+15375 out=+4

you> Now multiply that by 2. Just the number.

   782
   [gateway] served_by=accounts/fireworks/models/glm-5p2  requests=+1  in=+15386 out=+28
```

Follow-ups keep conversation context (`391` → `782` above), via `claude --continue`.

| | |
|---|---|
| `./demo.sh` | interactive prompt |
| `./demo.sh --canned` | three scripted questions, no typing |
| `./demo.sh "a question" "another"` | one-shot, non-interactive |
| `/new` | drop context, start a fresh conversation |
| `/log` | full per-request table for the session |
| `exit` | quit |

The ~15k input tokens per request is Claude Code's system prompt and tool
definitions — normal, and a good thing to point at when discussing cost.

**The per-question proof reads the metrics endpoint, not the access log.** Envoy
buffers its access log and flushes on a ~10s timer (measured), so an
access-log-based proof lands two questions late in a live session.
`http://localhost:1064/metrics` updates instantly. `/log` still reads the access
log, and will lag the last request by about ten seconds.

**Do not ask "what model are you?"** It looks like the obvious demo beat and it
backfires: Claude Code's system prompt tells the model it is Claude, and GLM
complies — it answers *"I'm Claude, built by Anthropic."* Verified against the
live endpoint. The access log is the provenance proof, not the model's
self-report; `served_by=accounts/fireworks/models/glm-5p2` is unambiguous.

Two cosmetic warnings on first run, both harmless: Claude Code notes that
claude.ai connectors are disabled (because an auth token is set), and that it
doesn't recognize the deployment name so it assumes a 200k context window. Set
`GLM_CONTEXT_TOKENS` in `.env` to silence the second one.

## Gotchas that cost real debugging time

**The Anthropic API is served at `/anthropic/v1/messages`, not `/v1/messages`.**
This is not in the docs. The extproc registers processors by *exact path*, and
the Anthropic one lands under `/anthropic`. POSTing to `/v1/messages` returns
`404 unsupported path: /v1/messages`. So `ANTHROPIC_BASE_URL` must be
`http://localhost:1975/anthropic` — Claude Code appends `/v1/messages` itself.
To see the registered paths on any version: `./bin/aigw run <config> --debug 2>&1 | grep "Registering processor"`.

**Auth uses `AzureAPIKey`, not `APIKey`.** `type: APIKey` sends
`Authorization: Bearer`; Foundry wants the `api-key` header. `type: AzureAPIKey`
does that. Use the **Azure** key from the Foundry portal — a Fireworks `fw_...`
or Fire Pass `fpk_...` key will not authenticate.

**The model name must be rewritten.** Claude Code sends `claude-opus-5` etc. in
the request body. Foundry would 404 on that, so the `AIServiceBackend` has a
`bodyMutation` pinning `model` to `FW-GLM-5.2` regardless of what came in.
`claude-foundry.sh` also sets the `ANTHROPIC_*_MODEL` slots so the access log and
Claude Code's status line agree with reality.

**The 32KiB default buffer limit will truncate Claude Code.** It sends whole files
and long transcripts. The `ClientTrafficPolicy` raises it to 50MiB.

**The model name is the *deployment* name, which usually carries a suffix.**
The portal deploys `FW-GLM-5.2` as e.g. `FW-GLM-5.2-standard`. Using the bare
model name gets `404 DeploymentNotFound` — the resource and key are fine, the
name isn't. List the real ones:

```bash
curl -s "https://$FOUNDRY_HOST/openai/deployments?api-version=2023-03-15-preview" \
  -H "api-key: $AZURE_API_KEY" | python3 -m json.tool
```

Put that value in `FOUNDRY_MODEL`; the config reads it via `${FOUNDRY_MODEL}`.

**`wellKnownCACertificates: "System"` does not work under `aigw run` on macOS.**
It resolves to a Linux CA path that doesn't exist, and every upstream request
fails with `TLS_error:_Secret_is_not_supplied_by_SDS` → HTTP 503. Dropping the
`BackendTLSPolicy` entirely is worse (plaintext to :443 → `connection
termination`). The fix: `caCertificateRefs` pointing at a `system-ca` ConfigMap
that `start-gateway.sh` builds from the host's real bundle (`/etc/ssl/cert.pem`
on macOS) and appends to a generated config. In a Linux cluster the plain
`"System"` form works and this scaffolding is unnecessary.

**GLM 5.2 is a reasoning model — give it token headroom.** It spends output
tokens thinking before emitting visible content. At `max_tokens: 64` the entire
budget goes to reasoning: no content block is produced, the stream ends right
after `message_start` with `stop_reason: max_tokens`, and it reads like a broken
gateway. The smoke test uses 2048. Claude Code sets its own generous limit, so
this only bites hand-rolled curl tests.

## Status

Verified against the live Foundry deployment `FW-GLM-5.2-standard` on
`<your-resource>.services.ai.azure.com`: **11/11 smoke tests pass**, and
Claude Code answers questions through the gateway with GLM 5.2 serving.

## Verifying without Azure credentials

`test/` contains an offline twin: a mock Foundry that records every upstream
request, plus a gateway config pointed at it instead of Azure.

```bash
./test/run-offline-test.sh
```

This proves the translation independently of your Azure setup. Current result:

```
  PASS  admin /health reachable
  PASS  data plane on :1975 answering
  PASS  response is valid JSON
  PASS  Anthropic Message envelope (type/role/content[])
  PASS  usage.input_tokens / output_tokens populated
  PASS  SSE emits message_start / content_block_delta / message_delta / message_stop
  PASS  tool_use block returned in Anthropic shape
  PASS  stop_reason == tool_use
  11 passed, 0 failed
```

and the recorded upstream requests confirm the translation:

| Anthropic in | OpenAI out |
|---|---|
| `POST /anthropic/v1/messages` | `POST /openai/v1/chat/completions` |
| `"model": "claude-opus-5"` | `"model": "FW-GLM-5.2"` (bodyMutation) |
| `max_tokens` | `max_completion_tokens` |
| `tools[].input_schema` | `tools[].function.parameters` |
| `tool_choice: {"type":"any"}` | `tool_choice: "required"` |
| `stream: true` | `stream: true` + `stream_options.include_usage` |
| — | `api-key: <AZURE_API_KEY>` header |

## Files

| File | |
|---|---|
| `aigw-foundry.yaml` | Gateway config — the real one, points at Azure |
| `start-gateway.sh` | Loads `.env`, validates it, runs `aigw` |
| `demo.sh` | Interactive prompt; prints gateway proof after each answer |
| `claude-foundry.sh` | Launches interactive Claude Code with the right env |
| `smoke-test.sh` | 11 assertions against a running gateway |
| `test/aigw-mock.yaml` | Offline twin of the config (localhost upstream) |
| `test/mock_foundry.py` | Mock Foundry that records upstream requests |
| `test/run-offline-test.sh` | Starts both, runs the smoke test, tears down |
| `eval/` | Bug-fix eval: CSV→JSONL converter, runner, results — see `eval/README.md` |
| `bin/aigw` | Envoy AI Gateway CLI v1.0.0 (gitignored) |

## Evaluating the model

`eval/` converts `code_bug_fix_pairs.csv` to JSONL and grades GLM 5.2 through
the gateway. **Read `eval/README.md` first** — the CSV's 1000 rows are 10
templates repeated ~100× each, and three of the ten are broken as test cases.

```bash
python3 eval/csv_to_jsonl.py --clean --dedup
python3 eval/run_eval.py eval/bug_fix_clean_dedup.jsonl
```

## Cost, cache hit rate, TTFT, latency percentiles

Azure Monitor reports none of these for Fireworks models — its cost panel says
*"Cost monitoring is available for Foundry Models sold directly by Azure only."*
The gateway is in the request path and measures all of it.

```bash
python3 eval/gateway_stats.py                      # cost, cache, p50/p90/p95/p99, TTFT
python3 eval/cache_probe.py --prefix-tokens 15000  # true cache hit rate
```

⚠ **The gateway under-reports prompt cache, so its cost figure is an upper
bound.** aigw v1.0.0 drops the upstream `prompt_tokens_details.cached_tokens`
in translation. Measured on this deployment: an identical prefix reports
`cached_tokens=0` through the gateway and `5,613` queried directly. At Claude
Code's prompt size the real steady-state hit rate is **100%**, making the input
cost roughly **10× lower** than the gateway suggests. Details in
`eval/README.md`.

**Cache routing keys work through Foundry** — measured, previously undocumented.
`prompt_cache_key` and `user` both survive the hop and partition the prompt
cache (same key 100% hit, different key 0%). The gateway now injects a
fleet-wide `prompt_cache_key` on every request via `CACHE_KEY` in `.env`. Use
one key for the whole fleet, not per user — Claude Code users all share the same
~15k system prompt, so a per-user key would fragment it and lower the hit rate.
Reproduce with `python3 eval/cache_key_probe.py` — it runs a two-arm test
(same key vs different key) against a no-key baseline, paced under the
deployment's request-per-minute cap. Pacing matters: unpaced, `429` retries land
on cold replicas and the resulting random misses are indistinguishable from
cache partitioning, which is enough to make you conclude either answer.

Header inspection cannot verify this — Azure APIM strips every `fireworks-*`
response header. Only `usage.prompt_tokens_details.cached_tokens` in the
response body survives the hop.

## Moving this to PayPal's real gateway

`aigw run` and Kubernetes consume the same CRDs, so `aigw-foundry.yaml` mostly
transfers as-is. What changes: drop the `GatewayClass`/`Gateway`/`EnvoyProxy`
blocks in favour of PayPal's existing `Gateway`, point `parentRefs` at it, and
move the `Secret` to whatever secret store the cluster uses. The
`AIGatewayRoute`, `AIServiceBackend`, `Backend`, `BackendTLSPolicy`, and
`BackendSecurityPolicy` blocks carry over unchanged.

Worth adding for a real deployment, all supported by the same CRDs:
token-based rate limiting per team (`llmRequestCosts` is already wired up and
emitting into the access log), and a second `AIServiceBackend` with
`priority` for failover to `api.fireworks.ai` — which, being Anthropic-native,
needs no translation.
