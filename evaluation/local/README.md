# Bug-fix eval — `code_bug_fix_pairs.csv` → JSONL → GLM 5.2

```bash
python3 evaluation/local/csv_to_jsonl.py --clean --dedup          # build the dataset
./start-gateway.sh                                    # terminal 1
python3 evaluation/local/run_eval.py evaluation/local/bug_fix_clean_dedup.jsonl
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

## Files

| | |
|---|---|
| `csv_to_jsonl.py` | CSV → JSONL, with `--clean` / `--dedup` / `--keep-marker` |
| `run_eval.py` | Runs a JSONL against the gateway, grades, writes per-case results |
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
code generation, SWE-bench for real bug
fixing, and keep this one as a smoke test.
