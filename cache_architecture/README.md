# Prompt cache

What every turn costs in tokens, and how much of it came out of GLM 5.2's
prompt cache.

```bash
./cache_architecture/demo_cache_gateway.sh   # through the gateway — the real path
./cache_architecture/demo_cache_direct.sh    # straight to Foundry
python3 cache_architecture/cache_monitor.py  # measure a real Claude Code fleet
```

Run them from anywhere; they locate the repo root themselves.

## None of the demos run Claude Code

Worth stating up front, because the request bodies look like they do.

| | runs the `claude` CLI? | what builds the request |
|---|---|---|
| `demo_cache_gateway.sh` | no | an inline Python heredoc |
| `demo_cache_direct.sh` | no | an inline Python heredoc |
| `cache_monitor.py` | no — but it **records** Claude Code once | Python |
| *(repo root)* `demo.sh`, `claude-foundry.sh` | **yes** | Claude Code |

`demo_cache_gateway.sh` sends **the same Anthropic Messages request to the same
gateway endpoint** Claude Code uses, so the gateway does identical work. Only
the sender differs.

That is deliberate. Claude Code sends 24,074 tokens of system prompt and tool
definitions on every request — it would swamp the token figures these scripts
exist to show. They send a ~500-token prompt so the numbers stay readable. To
measure the real thing, use `cache_monitor.py` below.

Two details that keep causing the same question:

- **`"model": "claude-opus-5"`** in the request body is a required field that is
  immediately discarded. The gateway's `bodyMutation` overwrites it with
  `FOUNDRY_MODEL` before the request leaves your laptop. It is set to the string
  Claude Code really sends so the access log shows the rewrite.
- **No Anthropic model is involved anywhere.** Translation is deterministic Go
  code in the gateway's external processor — field renames, not inference. There
  is no Anthropic credential in this repo.

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
sends **24,074 tokens** of instructions and tool definitions on every single
turn — measured with `cache_monitor.py` below — identical each time, before the
user has typed anything. That fixed prefix dwarfs the conversation, which is
exactly why caching it is worth doing.

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

Every user sends the same ~24k system prompt. One shared key keeps them all on
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

The gateway cannot tell you how much was cached — **anywhere**. Envoy AI Gateway
v1.0.0 drops the upstream OpenAI field
`usage.prompt_tokens_details.cached_tokens` in translation, so the Anthropic
response it returns always says zero even on a request GLM served entirely from
cache. The metrics endpoint is no help either, despite `aigw-foundry.yaml`
requesting the figure via `llmRequestCosts: CachedInputToken`:

```
$ curl -s localhost:1064/metrics | grep -o 'gen_ai_token_type="[a-z_]*"' | sort -u
gen_ai_token_type="input"
gen_ai_token_type="output"          # there is no "cached" type
```

Measured directly: two identical requests three seconds apart, both returning
`cache_read_input_tokens: 0` through the gateway, while the same prefix queried
straight to Foundry reported 24,073 tokens cached.

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

## Measuring a real fleet — `cache_monitor.py`

The probe trick does not transfer to Claude Code. It works only because the
script knows its own prefix and can replay it byte for byte; you cannot
intercept Claude Code's prefix mid-conversation and do the same.

So measure from the side. Capture the prefix once, then re-send it on a timer
straight to Foundry:

```bash
python3 cache_architecture/cache_monitor.py capture
# in another terminal — one throwaway message through the recorder:
ANTHROPIC_BASE_URL=http://localhost:1976/anthropic claude -p hi

python3 cache_architecture/cache_monitor.py watch --interval 60
```

`capture` stands a recorder up on port 1976 in place of the gateway, keeps the
`system` and `tools` fields off the first request that arrives, and returns a
canned reply so the CLI exits cleanly. Measured on this deployment:

```
prefix    captured prefix (.claude-code-prefix.json) · ~25,373 tokens

08:37:31  cached      10 /  24,074  (  0.0%)  1,591ms  cold
08:37:47  cached  24,073 /  24,074  (100.0%)    807ms  HIT
08:38:03  cached  24,073 /  24,074  (100.0%)    821ms  HIT
08:38:19  cached  24,073 /  24,074  (100.0%)  2,994ms  HIT
```

**24,074 tokens** — a system prompt plus 26 tool definitions, byte-identical on
every request and across every developer. Once warm, all but one token of it
costs a tenth of the uncached rate. That single number is the entire argument
for one fleet-wide `CACHE_KEY`.

Options: `--interval` (default 60s), `--samples N` (default 0, run until
Ctrl-C), `--prefix FILE`, `--out FILE`. Without a captured prefix it falls back
to a synthetic one of similar size and says so.

Every sample appends to a CSV — `ts,prompt_tokens,cached_tokens,pct,latency_ms,status`.
`429`s are logged but kept out of the hit rate: a rate limit is not a cache
miss, and counting it as one manufactures hit-rate collapses that never
happened. At a 4-second interval this deployment's 66 req/min cap produces them
steadily; at the 60s default it does not.

**What it does and does not tell you.** It tracks whether that prefix stays
resident on the replicas your key routes to — the thing that actually varies,
and a good leading indicator. It is *not* a per-user hit rate: a developer whose
conversation has grown well past the captured prefix is not represented. Your
Azure invoice remains the authoritative number.

`.claude-code-prefix.json` is gitignored. It is Claude Code's own system prompt
and tool definitions — measurement input, and not ours to republish.

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
