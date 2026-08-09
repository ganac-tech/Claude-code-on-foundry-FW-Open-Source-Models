# Claude Code → Envoy AI Gateway → Foundry (GLM 5.2)

Run unmodified **Claude Code** against a Fireworks **GLM 5.2** deployment in
**Microsoft Foundry**.

![Architecture: Claude Code to Fireworks GLM 5.2 in Microsoft Foundry via a local Envoy AI Gateway](architecture/architecture.svg)

Claude Code speaks the Anthropic Messages API. Foundry serves Fireworks models
over OpenAI chat-completions only. A local Envoy AI Gateway translates between
them — streaming, tool calls and reasoning blocks included.

---

## Step 1 — Get your Foundry details

From the [Foundry portal](https://ai.azure.com/) → **Project settings**:

- **Endpoint**, e.g. `https://my-resource.services.ai.azure.com`
- **API key** (the Azure key — a Fireworks `fw_...` key will not work)

You also need the **deployment name**, which is usually *not* the plain model
name — the portal commonly appends a suffix like `FW-GLM-5.2-standard`. List
yours:

```bash
curl -s "https://<your-resource>.services.ai.azure.com/openai/deployments?api-version=2023-03-15-preview" \
  -H "api-key: <your-azure-key>" | python3 -m json.tool
```

## Step 2 — Install the gateway binary

```bash
mkdir -p bin
curl -fL -o bin/aigw \
  https://github.com/envoyproxy/ai-gateway/releases/download/v1.0.0/aigw-darwin-arm64
chmod +x bin/aigw
```

Swap `darwin-arm64` for `linux-amd64` or `linux-arm64` as needed. It is ~290 MB
and pulls its own Envoy on first run. No Docker, no Kubernetes.

## Step 3 — Configure

```bash
cp .env.example .env
```

Fill in three values:

```bash
FOUNDRY_HOST=my-resource.services.ai.azure.com   # paste the full URL if easier; it gets normalized
AZURE_API_KEY=<your-azure-key>
FOUNDRY_MODEL=FW-GLM-5.2-standard                # the DEPLOYMENT name from step 1
```

## Step 4 — Start the gateway

```bash
./start-gateway.sh
```

Leave it running. It prints where it is listening and what it is talking to:

```
messages -> http://localhost:1975/anthropic/v1/messages
upstream -> https://my-resource.services.ai.azure.com/openai/v1/chat/completions
model    -> FW-GLM-5.2-standard   (Foundry deployment name)
```

## Step 5 — Verify it works

In a second terminal:

```bash
./smoke-test.sh
```

Eleven checks covering non-streaming, streaming SSE, and tool calls. Run this
before involving Claude Code — it separates gateway problems from Claude Code
problems.

```
  PASS  response is valid JSON
  PASS  Anthropic Message envelope (type/role/content[])
  PASS  SSE emits message_start / content_block_delta / message_delta / message_stop
  PASS  tool_use block returned in Anthropic shape
  11 passed, 0 failed
```

## Step 6 — Use it

Interactive prompt:

```bash
./demo.sh
```

```
you> What is 17 * 23? Reply with just the number.

   391
   [gateway] served_by=accounts/fireworks/models/glm-5p2  requests=+1  in=+15375 out=+4

you> Now multiply that by 2. Just the number.

   782
   [gateway] served_by=accounts/fireworks/models/glm-5p2  requests=+1  in=+15386 out=+28
```

Follow-ups keep context. `/new` resets, `/log` shows the request log, `exit` quits.

Or launch a full interactive Claude Code session:

```bash
./claude-foundry.sh
```

---

## Which scripts actually run Claude Code

Worth being precise about, because only two of them do.

| script | runs the `claude` CLI? | what sends the request |
|---|---|---|
| `demo.sh` | **yes** — `claude -p "$q"` | Claude Code |
| `claude-foundry.sh` | **yes** — `exec claude` | Claude Code |
| `smoke-test.sh` | no | `curl` |
| `cache_architecture/demo_cache_gateway.sh` | no | a Python heredoc |
| `cache_architecture/demo_cache_direct.sh` | no | a Python heredoc |
| `cache_architecture/cache_monitor.py` | no (but it *records* Claude Code) | Python |

Every one of them speaks the **same Anthropic Messages API to the same gateway
endpoint**, so the gateway cannot tell them apart and does identical work for
each. The difference is only who builds the request.

The cache and test scripts skip Claude Code deliberately. Claude Code sends
**24,074 tokens** of system prompt and tool definitions on every request
(measured — see below), which would swamp the token numbers those scripts exist
to show. They send a ~500-token prompt instead so the figures stay legible.

Nothing here calls an Anthropic model. `"model": "claude-opus-5"` appears in the
request bodies because the field is required and Claude Code really sends it —
the gateway's `bodyMutation` overwrites it with `FW-GLM-5.2-standard` before the
request leaves your laptop. There is no Anthropic credential in this repo;
Claude Code is given the literal placeholder `gateway-injected`.

### Watching the prompt cache

*(These two do not use Claude Code — see the table above.)*

`cache_architecture/demo_cache_direct.sh` shows what every turn cost in tokens, including how much came
out of the prompt cache:

```
you> What is 17 * 23? Reply with just the number.
   391
   ─ in 554  ·  cached 0 (0%)  ·  out 23  ·  1,043ms ─ cold

you> /again
   391
   ─ in 574  ·  cached 553 (96%)  ·  out 23  ·  1,036ms ─ CACHE HIT
```

`/again` re-sends the last prompt, `/newkey` rotates the cache key so the same
prompt starts cold again, and `/stats` totals the session with what the cache
saved.

There are two versions, because **the gateway cannot report cache reads at
all**. Envoy AI Gateway v1.0.0 drops the upstream
`usage.prompt_tokens_details.cached_tokens` field in translation, and it is
missing from the metrics endpoint too — even though `aigw-foundry.yaml` asks for
it via `llmRequestCosts: CachedInputToken`:

```
$ curl -s localhost:1064/metrics | grep -o 'gen_ai_token_type="[a-z_]*"' | sort -u
gen_ai_token_type="input"
gen_ai_token_type="output"          # no "cached" type exists
```

So asking the gateway how much was cached always returns zero — even on a
request GLM served entirely from cache.

| | traffic path | where the cached number comes from |
|---|---|---|
| `cache_architecture/demo_cache_direct.sh` | direct to Foundry | the response itself |
| `cache_architecture/demo_cache_gateway.sh` | **through the gateway** | a one-token probe |

The gateway one is the realistic one: your prompt takes the same path
Claude Code takes, and GLM does the caching. It starts the gateway for you — no
separate terminal — and asks which cache key to use first:

```
Pick a cache key
   1) k1      2) k2      3) k3
key> 1

you> What is 17 * 23? Reply with just the number.
   391
   ─ in 537  ·  cached 536 (100%)  ·  out 74 ─ CACHE HIT

you> /key            # switch to k2, gateway restarts
you> What is 17 * 23? Reply with just the number.
   391
   ─ in 517  ·  cached 10 (2%)  ·  out 6 ─ cold
```

Before each turn it sends a one-token probe straight to Foundry with the same
prefix and the same key the gateway injects, and reads the cache meter off that.
Input and output tokens still come from the gateway's own response.

Switching keys restarts the gateway because the key cannot come from the client.
`prompt_cache_key` is not a field in the Anthropic Messages API, so a client that
sends one has it dropped in translation — measured. It has to be injected by the
gateway's own config.

Caching is keyed on `CACHE_KEY` from `.env`, which the gateway stamps onto every
request. It is deliberately one key for the whole fleet: every Claude Code user
sends the same large system prompt, so a shared key lets them all reuse one
cached copy of it. A key per user would split it up and cost more.

### Measuring a real Claude Code fleet

The two demos above measure their own prompts. Neither can measure Claude Code,
because the per-turn probe only works when the script knows its own prefix and
can replay it — you cannot do that to Claude Code.

`cache_architecture/cache_monitor.py` measures it from the side. Capture the
prefix Claude Code actually sends, once, then re-send it on a timer straight to
Foundry and read the cache meter off the response:

```bash
python3 cache_architecture/cache_monitor.py capture
# in another terminal, one throwaway message through the recorder:
ANTHROPIC_BASE_URL=http://localhost:1976/anthropic claude -p hi

python3 cache_architecture/cache_monitor.py watch --interval 60
```

```
prefix    captured prefix (.claude-code-prefix.json) · ~25,373 tokens

08:37:31  cached      10 /  24,074  (  0.0%)  1,591ms  cold
08:37:47  cached  24,073 /  24,074  (100.0%)    807ms  HIT
08:38:03  cached  24,073 /  24,074  (100.0%)    821ms  HIT
```

Claude Code's fixed prefix is **24,074 tokens** — its system prompt plus 26 tool
definitions, identical on every request and identical across every developer.
Once warm, all but one token of it is served from cache at a tenth of the input
price. That is the whole argument for a single fleet-wide `CACHE_KEY`.

Every sample lands in a CSV (`ts,prompt_tokens,cached_tokens,pct,latency_ms,status`).
`429`s are recorded but excluded from the hit rate — a rate limit is not a cache
miss, and counting it as one invents hit-rate collapses that never happened.

**What this does and does not tell you.** It tracks whether that prefix stays
resident on the replicas your cache key routes to, which is the thing that
actually varies. It is not a per-user hit rate: a developer whose conversation
has grown well past the captured prefix is not represented. Treat it as a
leading indicator, and your Azure invoice as the authoritative number.

The captured prefix file is gitignored — it is Claude Code's own system prompt,
and not ours to republish.

---

## What the translation actually does

Everything below is captured from a live run — Claude Code's request on the
left, what Foundry receives and returns on the right.

### Request: Anthropic → OpenAI

| Anthropic (in) | OpenAI (out) |
|---|---|
| `POST /anthropic/v1/messages` | `POST /openai/v1/chat/completions` |
| `"model": "claude-opus-5"` | `"model": "FW-GLM-5.2-standard"` — pinned by `bodyMutation` |
| `"system": "You are a weather assistant."` | prepended to `messages` as `{"role":"system", ...}` |
| `"max_tokens": 1024` | `"max_completion_tokens": 1024` |
| `tools[].input_schema` | `tools[].function.parameters`, wrapped in `{"type":"function","function":{…}}` |
| `"tool_choice": {"type":"any"}` | `"tool_choice": "required"` |
| `"stream": true` | `"stream": true` + `"stream_options":{"include_usage":true}` |
| — | `api-key: <AZURE_API_KEY>` header injected |

### Response: OpenAI → Anthropic

What Foundry returns:

```json
{
  "choices": [{
    "message": {
      "content": "",
      "reasoning_content": "The user wants the weather in Paris. I'll call get_weather...",
      "tool_calls": [{
        "id": "chatcmpl-tool-a9cd45",
        "function": {"name": "get_weather", "arguments": "{\"city\": \"Paris\"}"}
      }]
    },
    "finish_reason": "tool_calls"
  }],
  "usage": {"prompt_tokens": 173, "completion_tokens": 34}
}
```

What Claude Code receives:

```json
{
  "type": "message",
  "role": "assistant",
  "content": [
    {"type": "thinking", "thinking": "The user wants the weather in Paris. I'll call get_weather..."},
    {"type": "tool_use", "id": "chatcmpl-tool-bd2efe", "name": "get_weather",
     "input": {"city": "Paris"}}
  ],
  "stop_reason": "tool_use",
  "usage": {"input_tokens": 173, "output_tokens": 37}
}
```

| OpenAI | Anthropic |
|---|---|
| `message.content` | `content[]` block of `{"type":"text"}` |
| `message.reasoning_content` | `content[]` block of `{"type":"thinking"}` |
| `message.tool_calls[]` | `content[]` block of `{"type":"tool_use"}` |
| `function.arguments` — a JSON **string** | `input` — a parsed **object** |
| `finish_reason: "stop"` | `stop_reason: "end_turn"` |
| `finish_reason: "tool_calls"` | `stop_reason: "tool_use"` |
| `finish_reason: "length"` | `stop_reason: "max_tokens"` |
| `usage.prompt_tokens` | `usage.input_tokens` |
| `usage.completion_tokens` | `usage.output_tokens` |

The `reasoning_content` → `thinking` mapping is what makes a reasoning model
like GLM 5.2 usable from Claude Code at all — without it the reasoning would be
dropped or leak into the visible answer.

### Streaming

One OpenAI chunk stream becomes the Anthropic SSE event sequence:

```
event: message_start        {"message":{"id":…,"model":"accounts/fireworks/models/glm-5p2",…}}
event: content_block_start  {"index":0,"content_block":{"type":"text","text":""}}
event: content_block_delta  {"index":0,"delta":{"type":"text_delta","text":"one"}}
event: content_block_delta  {"index":0,"delta":{"type":"text_delta","text":" two"}}
event: content_block_stop   {"index":0}
event: message_delta        {"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":103}}
event: message_stop
```

Token usage arrives once, in the final `message_delta` — the `usage` on
`message_start` is always zero.

## Files

| | | runs Claude Code |
|---|---|---|
| `aigw-foundry.yaml` | Gateway config | — |
| `start-gateway.sh` | Loads `.env`, runs the gateway | — |
| `demo.sh` | Interactive prompt | **yes** |
| `claude-foundry.sh` | Full Claude Code session | **yes** |
| `cache_architecture/` | Prompt-cache demos and monitor — see its README | no |
| `smoke-test.sh` | 11 checks against a running gateway | no |
| `architecture/` | Diagram and how the pieces fit — see `architecture/README.md` | — |
| `evaluation/` | Azure Foundry + local evaluation — see `evaluation/README.md` | no |

## Running it in Kubernetes

`aigw run` and Kubernetes consume the same CRDs, so `aigw-foundry.yaml` mostly
transfers as-is. Drop the `GatewayClass` / `Gateway` / `EnvoyProxy` blocks in
favour of your existing `Gateway`, point `parentRefs` at it, and move the
`Secret` to your cluster's secret store. The `AIGatewayRoute`,
`AIServiceBackend`, `Backend`, `BackendTLSPolicy` and `BackendSecurityPolicy`
blocks carry over unchanged.

## License

Apache-2.0.
