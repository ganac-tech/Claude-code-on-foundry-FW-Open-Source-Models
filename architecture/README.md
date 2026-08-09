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

## Two things Azure does that you did not set up

You deployed a model, not a gateway — but Azure still handles the request on its
own servers before Fireworks sees it. You can tell from the headers that come
back, which are Azure's, not Fireworks':

```
apim-request-id: bcfeae9b-46cd-4f14-8fa2-0218e9e45734
x-ratelimit-limit-requests: 66
```

**1. Azure removes Fireworks' own headers.**
Fireworks normally reports things like cache hits in the response headers. Azure
strips those out. If you want that information, read it from the response body
instead — `usage.prompt_tokens_details.cached_tokens` tells you how much of your
prompt was cached.

**2. Azure limits you to 66 requests per minute.**
Not something you configured, and you cannot turn it off. Go over it and you get
`429 Too Many Requests`. The retries then land on a cold server, so answers come
back slower and cached prompts stop hitting — which looks like the model got
worse, when really you just sent requests too fast. Space them out.

## Editing the diagram

`architecture.svg` is plain hand-written SVG. No build step, no tools needed —
open it in a text editor and change it.
