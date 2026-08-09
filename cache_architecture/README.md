# Prompt cache

Two scripts that show what every turn costs in tokens, and how much of it came
out of GLM 5.2's prompt cache.

```bash
./cache_architecture/demo_cache_gateway.sh   # through the gateway — the real path
./cache_architecture/demo_cache_direct.sh    # straight to Foundry
```

Run them from anywhere; they locate the repo root themselves.

---

## Where the cache lives

Not in the gateway. Not in Azure. **Inside GLM 5.2, in the memory of whichever
replica serves your request.**

```
Claude Code ─▶ gateway ─▶ Foundry ─▶ GLM 5.2 replica
                                      └─ your prompt prefix is cached here
```

That single fact explains everything else on this page. The cache is per-replica
memory, not a shared tier — so whether you get a hit depends on landing on a
replica that has already seen your prefix.

## What actually gets cached

Your question, almost never. The **system prompt**, almost always.

The demo scripts send a ~500-token system prompt on every turn. A typical run:

```
in 528  ·  cached 512 (97%)  ·  out 19
```

That 512 is the system prompt. Your question is a few dozen tokens and barely
moves the number.

This is the real shape of the problem, not an artifact of the demo. Claude Code
sends roughly **15,000 tokens** of instructions and tool definitions on every
single turn, identical each time, before the user has typed anything. That fixed
prefix dwarfs the conversation — which is exactly why caching it is worth doing.

## The cache key is a routing hint, not a partition

Every request carries a `prompt_cache_key`. The gateway sets it from `CACHE_KEY`
in `.env`.

Same key means your requests keep landing where your prefix already is. That
part is reliable. What is **not** reliable is the reverse — a new key does not
dependably give you a cold start.

Measured on this deployment:

| prefix | key | cached |
|---|---|---|
| never sent before | key A, first call | 0.3% |
| never sent before | key A, repeat | **100%** |
| never sent before | key B (new) | **0%** |
| never sent before | key A again | **100%** |

So far so clean. But with a prefix that has been in heavy use:

| prefix | key | cached |
|---|---|---|
| sent hundreds of times | brand-new key | **100%** |

Both results fit one explanation. A fresh prefix exists on exactly one replica,
so a different key routes you elsewhere and you miss. A prefix in heavy use is
cached on many replicas, so a new key lands somewhere that already has it and
you hit anyway.

**If you want a genuinely cold start, change the content, not the key.** That is
what `/cold` does — it stamps a fresh marker into the system prompt, producing
bytes nothing has ever seen.

## Use one key for the whole fleet

The instinct is a key per user or per session. For Claude Code that is
backwards and costs money.

Every user sends the same ~15k system prompt. One shared key keeps them all on
the same cached copy. A key per user splits that into one entry per person, so
everyone pays their own cold start and nobody shares.

Per-user keys are right only when users have genuinely *different* long
prefixes — a per-tenant document context, say. Partition by what the prefix is,
not by who is asking.

## Hits are not deterministic

A repeat usually hits. Sometimes it does not, because you landed on a replica
that has not seen your prefix. You will see this in the demos: two identical
turns a second apart, one at 98% and the next at 0%.

Nothing is broken when that happens. Judge the cache on a run of turns, not on
one.

## Why there are two scripts

The gateway cannot tell you how much was cached. Envoy AI Gateway v1.0.0 does
not carry the upstream OpenAI field `usage.prompt_tokens_details.cached_tokens`
into the Anthropic response it returns, so asking it always yields zero — even
on a request GLM served entirely from cache.

| | traffic path | cached number from |
|---|---|---|
| `demo_cache_gateway.sh` | **gateway** — the real path | a one-token probe |
| `demo_cache_direct.sh` | straight to Foundry | the response itself |

`demo_cache_gateway.sh` works around the gap: before each turn it sends a
one-token probe straight to Foundry with the same prefix and the same key the
gateway injects, and reads the meter off that. Input and output tokens still
come from the gateway's own response, and the output line says which is which.

The probe runs *before* the turn on purpose. It warms the cache itself, so
probing afterwards would report a hit every time and the cold case would never
appear.

## Commands

| | |
|---|---|
| `/again` | re-send the last prompt — the clearest way to see a hit |
| `/cold` | new system prompt; forces a genuinely cold start on any key |
| `/new` | clear the conversation, keep the cached system prompt |
| `/key` | switch cache key (gateway version restarts the gateway) |
| `/stats` | session totals and what the cache saved |

Switching keys restarts the gateway because a client cannot choose its own key.
`prompt_cache_key` is not a field in the Anthropic Messages API, so a client
that sends one has it dropped in translation — measured twice against a fresh
prefix: the gateway's request never warmed the key the client asked for. The
key has to come from the gateway's own config.

## Costing

`/stats` prices the session at GLM 5.2 list rates — `$1.40` per million uncached
input tokens, `$0.14` cached, `$4.40` output. Cached input is **ten times
cheaper**, which is where the saving comes from.

Override with `GLM_PRICE_IN`, `GLM_PRICE_CACHED` and `GLM_PRICE_OUT` in `.env`
if you have a negotiated Azure rate. Your Azure invoice is the authoritative
number; this is an estimate for comparing options.
