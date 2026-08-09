# Foundry evaluation results

Drop exports from **Foundry portal → Evaluations → your run** here:

| From the portal | Save as |
|---|---|
| **Download results** | `results.json` (or `.csv`) — per-row query, ground_truth, response, scores |
| **Download user logs** | `user-logs.json` |
| **Raw JSON** (under *See all properties*) | `run-properties.json` |
| Screenshot of the metrics panel | `overall-metrics.png` |

Images and exports commit like any other file:

```bash
git add evaluation/foundry-run/
git commit -m "Add Foundry evaluation results"
git push
```

## Before committing an export

Check it for anything you would not publish — the exports can carry your
resource name, deployment name, subscription or project identifiers, and full
request/response bodies:

```bash
grep -oE "[a-z0-9-]+\.services\.ai\.azure\.com|/subscriptions/[0-9a-f-]+" \
  evaluation/foundry-run/*.json
```

Nothing here is secret by nature, but this repo is public, so it is worth a look
first.

## Summary of the recorded run

| | |
|---|---|
| Deployment | `FW-GLM-5.2-standard` |
| Completed | 2026-08-07 |
| Rows | 10 |
| F1Score | 100% (10/10) |
| Similarity | 100% (10/10) |
| ResponseCompleteness | 100% (10/10) |
| Tokens | 2,648 system / 26,925 evaluation |

Read `../README.md` before quoting those numbers — the dataset is ten repeated
templates of single-line syntax errors, and two of them are broken as test
cases, so 100% is the expected result for any current model rather than a
finding about this one.
