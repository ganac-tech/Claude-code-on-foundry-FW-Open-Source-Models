# Claude Code → Envoy AI Gateway → Foundry (GLM 5.2)

Run unmodified **Claude Code** against a Fireworks **GLM 5.2** deployment in
**Microsoft Foundry**.

```
Claude Code ──POST /anthropic/v1/messages──▶  aigw :1975  ──POST /openai/v1/chat/completions──▶  Foundry
   (Anthropic Messages API)                    [translate]              (OpenAI schema)              GLM 5.2
```

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

| | |
|---|---|
| `aigw-foundry.yaml` | Gateway config |
| `start-gateway.sh` | Loads `.env`, runs the gateway |
| `demo.sh` | Interactive prompt |
| `claude-foundry.sh` | Full Claude Code session |
| `smoke-test.sh` | 11 checks against a running gateway |
| `evaluation/` | Azure Foundry + local evaluation — see `evaluation/README.md` |

## Running it in Kubernetes

`aigw run` and Kubernetes consume the same CRDs, so `aigw-foundry.yaml` mostly
transfers as-is. Drop the `GatewayClass` / `Gateway` / `EnvoyProxy` blocks in
favour of your existing `Gateway`, point `parentRefs` at it, and move the
`Secret` to your cluster's secret store. The `AIGatewayRoute`,
`AIServiceBackend`, `Backend`, `BackendTLSPolicy` and `BackendSecurityPolicy`
blocks carry over unchanged.

## License

Apache-2.0.
