# Bug-fix eval — `code_bug_fix_pairs.csv` → JSONL → GLM 5.2

```bash
python3 eval/csv_to_jsonl.py --clean --dedup          # build the dataset
./start-gateway.sh                                    # terminal 1
python3 eval/run_eval.py eval/bug_fix_clean_dedup.jsonl
```

## Read this before trusting any number from this dataset

`code_bug_fix_pairs.csv` is synthetic and much smaller than its row count suggests.

**1000 rows are 10 templates repeated ~100× each.** The only thing distinguishing
duplicates is a trailing `# Sample ID: N` comment. Running all 1000 rows costs
1000 requests and roughly 380k tokens to learn exactly what 10 rows tell you.
`--dedup` keeps one row per template.

**Three of the ten templates are broken as test cases:**

| Flag | Rows | Problem |
|---|---|---|
| `no_op` | 111 | `def greet(name)` has no bug — `fixed_code` == `buggy_code`. Any edit scores wrong. |
| `rewrites_literal` | 96 | `def foo()` expects the string literal changed (`'Missing colon…'` → `'Fixed missing colon…'`) on top of the syntax fix. A correct minimal fix fails. |
| — | 1000 | `commit_message` is drawn from a pool of 10 strings assigned independently of the diff. Row 1's fix adds parentheses; its message says "Improved readability with proper indentation". Never use it as a label, prompt hint, or judge criterion. |

`commit_url` points at `github.com/open-source-repo/...`, which does not exist.

## Measured result

GLM 5.2 (`FW-GLM-5.2-standard` on Foundry), 8 clean templates:

```
  compiles         100.0%
  ast_match         87.5%
  exact             87.5%
  latency  p50 2607ms   p95 2762ms
  wall             7.4s
```

**The one "failure" is not a model failure.** Template 2 expects
`list = [...]` → `lst = [...]`. GLM correctly added the missing colon — the
actual syntax error — but left the variable name alone, because the prompt says
*"do not rename anything unless that is the bug"*. Shadowing the `list` builtin
is a style preference, not the bug the row is testing. **GLM fixed the real
syntax error in 8/8.**

If you want the rename counted, drop that clause from `PROMPT` in
`csv_to_jsonl.py` and regenerate — but then you are testing instruction-following
against an inconsistent rubric, since the other nine templates are pure syntax.

On all 10 templates including the broken ones the score is 70%, of which two of
the three failures are dataset defects. That 70% is the number not to quote.

## Grading

Three independent signals, because exact match alone misleads on code:

| | |
|---|---|
| `compiles` | reply is syntactically valid Python |
| `ast_match` | reply and expected parse to the same AST — ignores whitespace and formatting. **Trust this one.** |
| `exact` | byte-equal after stripping markdown fences. Brittle: a correct fix formatted differently scores 0. |

`run_eval.py` calls `/anthropic/v1/messages` directly rather than shelling out to
Claude Code. Same gateway, same Anthropic→OpenAI translation, but without ~15k
tokens of Claude Code system prompt and tool definitions per request — which
would dominate cost and could itself change the answers.

`--max-tokens` defaults to 2048. GLM 5.2 reasons before answering; at low limits
the whole budget goes to reasoning and you get empty replies that look like
failures.

## Cost, cache, and latency — what Azure Monitor won't give you

Foundry's cost panel says *"Cost monitoring is available for Foundry Models sold
directly by Azure only. Foundry Models from partners and community are not
supported."* Fireworks GLM is a partner model, so there is no cost, no TTFT, and
no percentile breakdown in the portal. The gateway is in the request path and
measures all of it.

```bash
python3 eval/gateway_stats.py                      # everything so far
python3 eval/gateway_stats.py --since 30m --json   # scriptable
python3 eval/cache_probe.py --prefix-tokens 15000  # true cache hit rate
```

```
  ── latency ─ exact, from Envoy access log ──────────────────
    total p50            2,339 ms
    total p90            9,835 ms
    total p95           12,685 ms
    total p99           79,633 ms

  ── time to first token ─ approx, histogram-interpolated ───
    TTFT p50             947.9 ms
    TTFT p90           2,365.0 ms
    TTFT p95           3,875.0 ms

    per-output-token p50    7.4 ms  (~135 tok/s)
```

Two sources with different precision, and the tool labels which is which:

| Source | Gives | Precision |
|---|---|---|
| Envoy access log | duration, token counts per request | **exact** — nearest-rank percentiles |
| aigw `/metrics` | TTFT, time-per-output-token | **approximate** — interpolated within histogram buckets. The only source; the access log has no TTFT. |

**TTFT is only recorded for streaming requests** (`"stream": true`) — a
non-streaming call has no first-token event to measure. Claude Code always
streams, so this populates from real traffic. Metrics are in-process counters
and reset when the gateway restarts; the access log persists per run.

### The gateway under-reports cache — cost is an upper bound

**Envoy AI Gateway v1.0.0 does not map the upstream OpenAI field
`usage.prompt_tokens_details.cached_tokens` into the Anthropic
`cache_read_input_tokens` it returns.** Every request looks 100% uncached in
gateway metrics even when Foundry served it from cache.

Verified on this deployment — same 5,616-token prefix, two paths:

| | `cached_tokens` |
|---|---|
| Through the gateway (`/anthropic/v1/messages`) | **0** |
| Direct to Foundry (`/openai/v1/chat/completions`) | **5,613** |

The caching is real; only the reporting is lost. `cache_probe.py` measures the
true rate by talking to Foundry directly. At Claude Code's actual prompt size:

```
  call     prompt   cached    hit%
  1        19,103    5,610   29.4%      <- cold, populates the cache
  2        19,103   19,099  100.0%
  3        19,103   19,099  100.0%

  steady-state hit rate (calls 2+): 100.0%
       billed as all-uncached : $0.080233
       with cache             : $0.008038   (90% lower)
```

So for a Claude Code workload the gateway's input-cost figure overstates by
roughly **10×**. Feed the measured rate back in:

```bash
python3 eval/gateway_stats.py --assume-cached-pct 100
```

`gateway_stats.py` prints this warning automatically whenever it sees zero
cached tokens, so the number is never quoted as fact by accident.

### Pricing

Defaults are Fireworks **direct-serverless list** rates for GLM 5.2 —
`$1.40` uncached input / `$0.14` cached input / `$4.40` output per 1M tokens.
**Foundry bills through Azure at your negotiated rate**, which will differ.
Override with `--price-in` / `--price-cached` / `--price-out`, or
`GLM_PRICE_IN` / `GLM_PRICE_CACHED` / `GLM_PRICE_OUT`. The Azure invoice is the
authoritative number; this is an estimate for comparing options.

## Files

| | |
|---|---|
| `csv_to_jsonl.py` | CSV → JSONL, with `--clean` / `--dedup` / `--keep-marker` |
| `run_eval.py` | Runs a JSONL against the gateway, grades, writes per-case results |
| `gateway_stats.py` | Cost, cache rate, TTFT + latency percentiles from the gateway |
| `cache_probe.py` | True cache hit rate, measured directly against Foundry |
| `cache_key_probe.py` | Does `prompt_cache_key`/`user` reach Fireworks? Two-arm test |
| `bug_fix.jsonl` | all 1000 rows |
| `bug_fix_clean.jsonl` | 793 rows — degenerate cases removed |
| `bug_fix_dedup.jsonl` | 10 rows — one per template |
| `bug_fix_clean_dedup.jsonl` | 8 rows — **start here** |
| `*.results.jsonl` | per-case output from a run |

## Output schema

```json
{
  "id": "1",
  "template_id": 1,
  "messages": [{"role": "user", "content": "Fix the bug in this Python code…"}],
  "expected": "x = [1, 2, 3]\nprint(x)",
  "buggy_code": "x = [1, 2, 3]\nprint x",
  "fixed_code": "x = [1, 2, 3]\nprint(x)",
  "flags": [],
  "commit_message": "Improved readability with proper indentation",
  "date": "2024-12-16"
}
```

`messages` is Anthropic/OpenAI chat format, so the file drops straight into most
eval harnesses. `flags` is empty for clean cases.

## If you need a real eval

This dataset can show the pipeline works end to end; it cannot differentiate
models. Ten templates of introductory Python syntax errors will be at or near
100% for any current model. For a result worth showing, use HumanEval / MBPP for
code generation, SWE-bench (or a PayPal-internal commit corpus) for real bug
fixing, and keep this one as a smoke test.
