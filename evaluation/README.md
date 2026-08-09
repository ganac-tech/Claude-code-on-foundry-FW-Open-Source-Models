# Evaluation

Two ways the GLM 5.2 deployment was evaluated on a Python bug-fixing dataset.

```
evaluation/
├── code_bug_fix_pairs.csv   the dataset (1000 rows)
├── foundry-run/             Azure Foundry Evaluations — results go here
└── local/                   run it yourself through the gateway
```

---

## Azure Foundry Evaluations

Run through the Foundry portal → **Optimize → Evaluations**, against the
`FW-GLM-5.2-standard` deployment.

### Result

| Metric | Score | |
|---|---|---|
| F1Score | **100%** | 10 / 10 |
| Similarity | **100%** | 10 / 10 |
| ResponseCompleteness | **100%** | 10 / 10 |

Token usage: 2,648 evaluated system tokens, 26,925 evaluation tokens.
Run completed 2026-08-07.

### How to reproduce

1. **Foundry portal** → your project → **Evaluations** → new evaluation.
2. Upload `code_bug_fix_pairs.csv` as the dataset.
3. Map the columns:
   - `query` ← the prompt (`Fix the bug in the following Python code: <buggy_code>`)
   - `ground_truth` ← `fixed_code`
   - `context` ← `commit_message` *(see the warning below — consider leaving this unmapped)*
4. Target: the `FW-GLM-5.2-standard` deployment.
5. Metrics: F1Score, Similarity, ResponseCompleteness.
6. Run, then **Download results** for the per-row detail.

Drop the exported results and any screenshots into `foundry-run/`.

---

## Reading those numbers honestly

**100% across three metrics is not evidence that the model is good at bug
fixing.** The dataset cannot produce any other outcome from a current model, for
three specific reasons.

### 1. The dataset is 10 templates, not 1000 rows

All 1000 rows are ten distinct `(buggy, fixed)` pairs repeated ~100× each. The
only thing that differs between duplicates is a trailing `# Sample ID: N`
comment. Evaluating 10 rows — as this run did — already covers the entire
variety in the file; evaluating all 1000 would cost 100× the tokens and tell you
exactly the same thing.

Every case is a single-line introductory Python syntax error: a missing colon,
`print x` → `print(x)`, one wrong indent, `=` vs `==`.

### 2. `context` is noise, and it was fed to the graders

`context` maps to `commit_message`, which is drawn from a pool of ten strings
assigned **independently of the actual diff**. Every row visible in the results
table has a `context` that does not describe its own fix:

| id | `context` says | the fix actually is |
|---|---|---|
| 655 | "Fixed bug in recursive function call" | nothing — input and expected output are identical |
| 115 | "Improved readability with proper indentation" | added a missing `:` |
| 26 | "Corrected conditional operator mistake" | added a missing `:` |
| 760 | "Fixed bug in recursive function call" | fixed an indent |
| 282 | "Corrected conditional operator mistake" | added a missing `:` |
| 251 | "Refactored variable naming for clarity" | added a missing `:` |
| 229 | "Refactored variable naming for clarity" | `=` → `==` |

Zero of seven match. Similarity and ResponseCompleteness are LLM-judged and can
consume `context`, so mapping this column feeds the judge misleading input.
Leave it unmapped.

### 3. Two of the ten templates are broken as test cases

Both appear in this run:

- **id 655** (`def greet(name)`) — `fixed_code` is **identical** to `buggy_code`.
  There is no bug. 111 rows share this template. A model that "fixes" it is
  wrong; one that echoes the input is right.
- **id 282** (`def foo()`) — the expected output changes the *string literal*
  (`'Missing colon…'` → `'Fixed missing colon…'`) on top of adding the colon. A
  correct minimal fix does not match. 96 rows share this template.

Scoring 100% on both means the metrics are lenient enough to pass a case with
nothing to fix and a case whose expected output is arbitrary. Useful to know
about the metrics; not evidence about the model.

### What this run does establish

That the pipeline works end to end: Foundry accepted the dataset, invoked the
deployment, and the model returned syntactically valid, on-task Python for every
row. As a smoke test that is a pass. As a model comparison it has no
discriminating power — swap in any current model and expect the same 100%.

For a result worth acting on, use **HumanEval** or **MBPP** for code generation
and **SWE-bench** (or a real internal commit corpus) for actual bug fixing.

---

## Local evaluation

`local/` runs the same dataset through the gateway instead, and grades with
three independent signals — including AST equivalence, which does not punish a
correct fix that is formatted differently.

```bash
python3 evaluation/local/csv_to_jsonl.py --clean --dedup
python3 evaluation/local/run_eval.py evaluation/local/bug_fix_clean_dedup.jsonl
```

It flags the broken templates rather than scoring them, so the number it reports
is the one you can defend. See `local/README.md`.
