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

## Things that will bite you

**The Anthropic API is at `/anthropic/v1/messages`, not `/v1/messages`.**
Undocumented. The gateway registers processors by exact path, so the root
returns `404 unsupported path`. This is why `ANTHROPIC_BASE_URL` must end in
`/anthropic` — Claude Code appends `/v1/messages` itself.

**Use the deployment name, not the model name.** `FW-GLM-5.2` gets
`404 DeploymentNotFound` when the deployment is actually `FW-GLM-5.2-standard`.
The endpoint and key are fine; only the name is wrong.

**Auth is `AzureAPIKey`, not `APIKey`.** Foundry wants the `api-key` header;
plain `APIKey` sends `Authorization: Bearer` and fails.

**`wellKnownCACertificates: "System"` does not work on macOS.** It resolves to a
Linux CA path that doesn't exist, and every request dies with
`TLS_error:_Secret_is_not_supplied_by_SDS` → HTTP 503. `start-gateway.sh` builds
a CA ConfigMap from the host bundle instead. On Linux the plain `"System"` form
works and this scaffolding is unnecessary.

**GLM 5.2 reasons before answering — give it token headroom.** At
`max_tokens: 64` the whole budget goes to reasoning, no content block is
produced, and it looks like a broken gateway. Claude Code sets its own generous
limit, so this only bites hand-rolled curl tests.

**Don't demo with "what model are you?"** Claude Code's system prompt tells the
model it is Claude, and GLM complies. The `[gateway] served_by=...` line is the
real proof.

Two harmless warnings on first run: claude.ai connectors disabled (because an
auth token is set), and an unrecognized model name (so a 200k context window is
assumed). Set `GLM_CONTEXT_TOKENS` in `.env` to silence the second.

## Files

| | |
|---|---|
| `aigw-foundry.yaml` | Gateway config |
| `start-gateway.sh` | Loads `.env`, runs the gateway |
| `demo.sh` | Interactive prompt |
| `claude-foundry.sh` | Full Claude Code session |
| `smoke-test.sh` | 11 checks against a running gateway |
| `eval/` | Optional bug-fix evaluation — see `eval/README.md` |

## Running it in Kubernetes

`aigw run` and Kubernetes consume the same CRDs, so `aigw-foundry.yaml` mostly
transfers as-is. Drop the `GatewayClass` / `Gateway` / `EnvoyProxy` blocks in
favour of your existing `Gateway`, point `parentRefs` at it, and move the
`Secret` to your cluster's secret store. The `AIGatewayRoute`,
`AIServiceBackend`, `Backend`, `BackendTLSPolicy` and `BackendSecurityPolicy`
blocks carry over unchanged.

## License

Apache-2.0.
