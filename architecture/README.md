# Architecture

![Claude Code to Fireworks GLM 5.2 in Microsoft Foundry, via a local Envoy AI Gateway](architecture.svg)

## The problem, in one line

Claude Code only knows how to talk to Anthropic. Foundry only knows how to answer
in OpenAI's format. Neither can change. So we put a translator in the middle.

## The four parts

**Claude Code** — the coding assistant you already use. Runs on your laptop.
Nothing about it is modified.

**Envoy AI Gateway** — the translator. Also runs on your laptop, on port 1975.
This is the piece that does all the work.

**Foundry deployment** — your model in Azure, named `FW-GLM-5.2-standard`.

**Fireworks GLM 5.2** — the actual model that writes the answer.

## What happens when you ask a question

Say you type *"fix this bug"* into Claude Code.

**Step 1 — Claude Code sends the question to your own machine.**
Normally it would send it to `api.anthropic.com`. We changed one setting
(`ANTHROPIC_BASE_URL`) so it sends to `localhost:1975` instead — the gateway.
Claude Code does not know anything is different.

**Step 2 — The gateway rewrites the request.**
It arrived in Anthropic's format. Azure won't understand that. So the gateway
changes it:

- renames the fields (`max_tokens` becomes `max_completion_tokens`, and so on)
- swaps the model name to your deployment name, `FW-GLM-5.2-standard`
- adds your Azure API key to the request

**Step 3 — The gateway sends it to Azure.**
Over the internet, encrypted, to your Foundry endpoint.

**Step 4 — Azure checks the key and hands it to your deployment.**

**Step 5 — GLM 5.2 writes the answer.**

**Step 6 — The answer travels back to the gateway.**
It comes back in OpenAI's format, which Claude Code cannot read.

**Step 7 — The gateway rewrites the answer.**
Back into Anthropic's format — the model's reasoning becomes a "thinking" block,
any tool it wants to run becomes a "tool use" block, and the token counts get
renamed.

**Step 8 — Claude Code shows you the answer.**
As far as it can tell, Anthropic replied.

## Why you see words appear one at a time

The answer is not sent in one lump. It streams — a few words at a time, as the
model writes them.

The gateway translates each piece as it passes through, rather than waiting for
the whole thing. That is why the first words show up in about half a second even
though the full answer takes a couple of seconds.

## Where your key lives

Your Azure key is only ever in the gateway. Claude Code is given a fake
placeholder token, because it insists on having *something*. The real key is
added by the gateway on its way out, so it never sits in Claude Code's config.

## Does anything go to Anthropic?

No. Your questions and the model's answers only ever travel between your laptop
and your Azure subscription.

Anthropic made the app you are typing into. They do not see what you type.

## Taking this to production

What you have now is one gateway on one laptop. That is right for proving it
works, and wrong for a team. Four steps turn it into a shared service — the
first two move the gateway, the last two decide how you buy the model.

### Step 1 — Put the gateway in a container

The gateway is stateless. It holds nothing between requests, so any copy can
serve any request and you can run as many as you like. That makes it easy to
containerize.

```dockerfile
FROM envoyproxy/ai-gateway-cli@sha256:<digest>
COPY aigw-foundry.yaml /etc/aigw/config.yaml
ENV AIGW_RUN_ID=0
EXPOSE 1975
CMD ["run", "/etc/aigw/config.yaml"]
```

Two things to change first:

- **Simplify the TLS setup.** `start-gateway.sh` builds a CA bundle by hand
  because macOS does not have the file Envoy expects. A Linux container does. In
  `aigw-foundry.yaml`, delete the `caCertificateRefs` block and put back
  `wellKnownCACertificates: "System"`.
- **Pin the image.** Only `latest` and per-commit tags are published — there are
  no version numbers. Pin a digest (`@sha256:...`) so a rebuild next month gives
  you the same gateway.

### Step 2 — Run it on Azure Container Apps

Container Apps is the least amount of infrastructure that still scales. No
cluster to operate, and it adds and removes copies of the gateway based on how
busy it is.

```bash
az containerapp create \
  --name aigw --resource-group <rg> --environment <aca-env> \
  --image <registry>.azurecr.io/aigw-foundry@sha256:<digest> \
  --target-port 1975 --ingress internal \
  --min-replicas 1 --max-replicas 10 \
  --scale-rule-name http --scale-rule-type http \
  --scale-rule-http-concurrency 20 \
  --secrets azure-api-key=<your-azure-key> \
  --env-vars \
    FOUNDRY_HOST=<resource>.services.ai.azure.com \
    FOUNDRY_MODEL=FW-GLM-5.2-standard \
    CACHE_KEY=claude-code-fleet \
    AZURE_API_KEY=secretref:azure-api-key
```

Notes on the choices above:

- **The key stops living in a file.** `secretref:` pulls it from Container Apps
  secrets. Point it at Key Vault instead if you already run one.
- **`--min-replicas 1`, not 0.** Scaling to zero saves money but the first
  request after an idle period waits for a cold start. For an interactive coding
  tool that is the wrong trade.
- **`--ingress internal` keeps it off the public internet**, which means
  developers reach it over your corporate network or VPN. If you make it
  external instead, put authentication in front of it — an open gateway is an
  open door to your Azure spend.
- **One shared `CACHE_KEY` for everyone.** Every Claude Code user sends the same
  large system prompt. One key lets them all reuse the same cached copy of it. A
  key per user would split that up and cost you more.

Then each developer points at the shared gateway instead of their own:

```bash
export ANTHROPIC_BASE_URL=https://aigw.<your-aca-domain>/anthropic
```

### Step 3 — Start on serverless

Keep the deployment you already have. Serverless (Foundry calls it PayGo, or
Data Zone Standard) bills per token with no commitment, which is what you want
while you still do not know the shape of your traffic.

It is capped, and you will meet the caps in this order:

- a **per-minute request limit on the deployment** — 66/min on this one, visible
  as `x-ratelimit-limit-requests` in any response header
- **500,000 tokens per minute** overall

Going over gives you `429 Too Many Requests`. The retries land on a server that
does not have your prompt cached, so answers get slower and your cache hit rate
drops. It reads like the model got worse; really you just sent requests faster
than the tier allows.

Ask for a quota increase at [aka.ms/fireworks-quota](https://aka.ms/fireworks-quota)
if you are close. Serverless is also **US Data Zone only** — East US, East US 2,
Central US, North Central US, West US, West US 3.

### Step 4 — Move to Provisioned Throughput once you know the load

Provisioned Throughput (PTU) is dedicated capacity. You pay per PTU-hour instead
of per token, and you get consistent latency, no shared rate limit, and
deployment in any region rather than the US only.

**Switch when the traffic is steady and predictable**, not before. PTU costs the
same whether you use it or not, so it only wins once you have a floor of demand.

The signals that you are ready:

- you hit `429` regularly during working hours
- token usage per hour has flattened into a recognisable daily shape
- latency variation is starting to matter to people

**The gateway is where you get the numbers to size it.** It sees every request
before Azure does, so it can tell you tokens per minute at peak, the ratio of
input to output, and how much of your input is cache hits — which is exactly
what PTU sizing needs. Ask Fireworks (sales@fireworks.ai) to size it with you;
guessing PTU counts is expensive in both directions.

You do not have to choose one. A common shape is PTU sized for the normal
weekday load, with serverless kept as overflow — the gateway can route to both,
so developers never notice which one answered.

## Editing the diagram

`architecture.svg` is plain hand-written SVG. No build step, no tools needed —
open it in a text editor and change it.
