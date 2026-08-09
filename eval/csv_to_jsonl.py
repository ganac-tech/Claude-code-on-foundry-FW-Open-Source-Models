#!/usr/bin/env python3
"""Convert code_bug_fix_pairs.csv to JSONL for model evaluation.

  python3 eval/csv_to_jsonl.py                      # all 1000 rows -> eval/bug_fix.jsonl
  python3 eval/csv_to_jsonl.py --clean              # drop broken cases
  python3 eval/csv_to_jsonl.py --dedup              # one row per distinct template (10)
  python3 eval/csv_to_jsonl.py --clean --dedup -o eval/bug_fix_clean.jsonl

Each output line:

  {
    "id":             "1",
    "template_id":    3,             # which of the 10 underlying templates
    "messages":       [{"role": "user", "content": "<instruction + buggy code>"}],
    "expected":       "<fixed_code>",
    "buggy_code":     "<...>",
    "fixed_code":     "<...>",
    "flags":          ["no_op"],     # see FLAGS below; empty = clean case
    "commit_message": "<...>",       # UNRELIABLE, see below
    "date":           "2024-12-16"
  }

FLAGS
  no_op              buggy_code == fixed_code. There is no bug. A model that
                     "fixes" it is marked wrong even though the input was valid.
  rewrites_literal   The expected output changes a string literal in addition to
                     the syntax fix, so a correct minimal fix fails exact match.

WHY commit_message IS UNRELIABLE
  It is drawn from a pool of 10 strings assigned independently of the actual
  diff. Row 1's fix adds parentheses to `print`; its message says "Improved
  readability with proper indentation". Do not use it as a label, a hint in the
  prompt, or a grading criterion.
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_ID_RE = re.compile(r"\n?# Sample ID: \d+\s*$")

PROMPT = (
    "Fix the bug in this Python code.\n"
    "Reply with only the corrected code — no explanation, no markdown fences.\n"
    "Make the minimal change necessary; do not rename anything or alter string "
    "literals unless that is the bug.\n\n"
    "{code}"
)


def strip_marker(s: str) -> str:
    """Remove the trailing '# Sample ID: N' comment that distinguishes duplicates."""
    return SAMPLE_ID_RE.sub("", s)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default=str(ROOT / "code_bug_fix_pairs.csv"))
    ap.add_argument("-o", "--out", default=None,
                    help="output path (default eval/bug_fix[_clean][_dedup].jsonl)")
    ap.add_argument("--clean", action="store_true",
                    help="drop rows flagged no_op or rewrites_literal")
    ap.add_argument("--dedup", action="store_true",
                    help="keep one row per distinct template (10 rows)")
    ap.add_argument("--keep-marker", action="store_true",
                    help="keep the '# Sample ID: N' comment in the prompt "
                         "(default strips it — it is not part of the bug)")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.csv, newline="", encoding="utf-8")))
    if not rows:
        print(f"error: no rows in {args.csv}", file=sys.stderr)
        return 1

    # Assign a stable template_id per distinct (buggy, fixed) pair, marker removed.
    template_ids: dict[tuple[str, str], int] = {}
    for r in rows:
        key = (strip_marker(r["buggy_code"]), strip_marker(r["fixed_code"]))
        template_ids.setdefault(key, len(template_ids) + 1)

    out_path = Path(args.out) if args.out else ROOT / "eval" / (
        "bug_fix"
        + ("_clean" if args.clean else "")
        + ("_dedup" if args.dedup else "")
        + ".jsonl"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    seen_templates: set[int] = set()
    written = 0
    counts = {"no_op": 0, "rewrites_literal": 0, "clean": 0}
    skipped = 0

    with out_path.open("w", encoding="utf-8") as fh:
        for r in rows:
            buggy_raw, fixed_raw = r["buggy_code"], r["fixed_code"]
            buggy = buggy_raw if args.keep_marker else strip_marker(buggy_raw)
            fixed = fixed_raw if args.keep_marker else strip_marker(fixed_raw)
            tid = template_ids[(strip_marker(buggy_raw), strip_marker(fixed_raw))]

            flags = []
            if strip_marker(buggy_raw) == strip_marker(fixed_raw):
                flags.append("no_op")
            # A changed string literal means exact match punishes a correct fix.
            if _literals(buggy_raw) != _literals(fixed_raw):
                flags.append("rewrites_literal")

            for f in flags:
                counts[f] += 1
            if not flags:
                counts["clean"] += 1

            if args.clean and flags:
                skipped += 1
                continue
            if args.dedup:
                if tid in seen_templates:
                    skipped += 1
                    continue
                seen_templates.add(tid)

            fh.write(json.dumps({
                "id": r["id"],
                "template_id": tid,
                "messages": [{"role": "user", "content": PROMPT.format(code=buggy)}],
                "expected": fixed,
                "buggy_code": buggy,
                "fixed_code": fixed,
                "flags": flags,
                "commit_message": r["commit_message"],  # unreliable, see module docstring
                "date": r["date"],
            }, ensure_ascii=False) + "\n")
            written += 1

    print(f"wrote {written} rows -> {out_path}", file=sys.stderr)
    if skipped:
        print(f"skipped {skipped} rows", file=sys.stderr)
    print(f"source: {len(rows)} rows, {len(template_ids)} distinct templates",
          file=sys.stderr)
    print(f"  clean            : {counts['clean']}", file=sys.stderr)
    print(f"  no_op            : {counts['no_op']}  (buggy == fixed; nothing to fix)",
          file=sys.stderr)
    print(f"  rewrites_literal : {counts['rewrites_literal']}"
          "  (expected output edits a string literal)", file=sys.stderr)
    return 0


def _literals(code: str) -> list[str]:
    """String literals in the snippet, so we can spot expected-output rewrites."""
    return re.findall(r"'[^']*'|\"[^\"]*\"", code)


if __name__ == "__main__":
    raise SystemExit(main())
