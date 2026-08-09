#!/usr/bin/env python3
"""Evaluate a model on the bug-fix JSONL through the local Envoy AI Gateway.

  ./start-gateway.sh                                   # terminal 1
  python3 evaluation/local/run_eval.py evaluation/local/bug_fix_clean_dedup.jsonl        # 8 cases, quick
  python3 evaluation/local/run_eval.py evaluation/local/bug_fix_clean.jsonl -n 100 -c 8  # 100 cases, 8 at a time

Talks to /anthropic/v1/messages directly rather than shelling out to Claude Code:
same gateway, same translation, but without ~24k tokens of Claude Code system
prompt and tools on every request — which would dominate cost and could itself
change the answers.

GRADING — three independent signals, because exact match alone is misleading
for code:
  compiles    the reply is syntactically valid Python
  ast_match   reply and expected parse to the same AST (ignores whitespace and
              formatting; this is the number to trust)
  exact       reply equals expected byte-for-byte after stripping markdown
              fences and trailing whitespace (brittle — a correct fix formatted
              differently scores 0)
"""
import argparse
import ast
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

FENCE_RE = re.compile(r"^\s*```(?:python|py)?\s*\n(.*?)\n\s*```\s*$", re.S)


def unfence(text: str) -> str:
    """Models wrap code in markdown fences even when told not to."""
    m = FENCE_RE.match(text.strip())
    return (m.group(1) if m else text).strip()


def ast_equal(a: str, b: str) -> bool:
    try:
        return ast.dump(ast.parse(a)) == ast.dump(ast.parse(b))
    except SyntaxError:
        return False


def compiles(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def call_gateway(base: str, model: str, messages: list, max_tokens: int,
                 timeout: int, retries: int = 2):
    """POST /v1/messages. Returns (text, error, latency_ms, in_tok, out_tok)."""
    body = json.dumps({"model": model, "max_tokens": max_tokens,
                       "messages": messages}).encode()
    last = None
    for attempt in range(retries + 1):
        t0 = time.time()
        req = urllib.request.Request(
            f"{base}/v1/messages", data=body,
            headers={"content-type": "application/json",
                     "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            ms = int((time.time() - t0) * 1000)
            text = "".join(b.get("text", "") for b in d.get("content", [])
                           if b.get("type") == "text")
            u = d.get("usage", {})
            return text, None, ms, u.get("input_tokens", 0), u.get("output_tokens", 0)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}"
        except Exception as e:  # timeout, connection reset, malformed JSON
            last = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    return "", last, 0, 0, 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dataset", help="input .jsonl from csv_to_jsonl.py")
    ap.add_argument("-o", "--out", default=None, help="per-case results .jsonl")
    ap.add_argument("-n", "--limit", type=int, default=0, help="only first N cases")
    ap.add_argument("-c", "--concurrency", type=int, default=4)
    ap.add_argument("--base", default="http://localhost:1975/anthropic")
    ap.add_argument("--model", default="glm",
                    help="ignored upstream — the gateway pins the real deployment "
                         "via bodyMutation; only affects logging")
    ap.add_argument("--max-tokens", type=int, default=2048,
                    help="GLM 5.2 reasons before answering; too low yields empty replies")
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    cases = [json.loads(l) for l in open(args.dataset, encoding="utf-8") if l.strip()]
    if args.limit:
        cases = cases[:args.limit]
    if not cases:
        print("error: dataset is empty", file=sys.stderr)
        return 1

    out_path = Path(args.out) if args.out else \
        Path(args.dataset).with_suffix(".results.jsonl")

    print(f"{len(cases)} cases · {args.concurrency} concurrent · {args.base}",
          file=sys.stderr)

    def run(case):
        text, err, ms, tin, tout = call_gateway(
            args.base, args.model, case["messages"], args.max_tokens, args.timeout)
        got = unfence(text)
        exp = case["expected"]
        return {
            "id": case["id"],
            "template_id": case["template_id"],
            "flags": case.get("flags", []),
            "error": err,
            "expected": exp,
            "got": got,
            "exact": (not err) and got == exp.strip(),
            "ast_match": (not err) and ast_equal(got, exp),
            "compiles": (not err) and compiles(got),
            "latency_ms": ms,
            "input_tokens": tin,
            "output_tokens": tout,
        }

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = []
        for i, r in enumerate(pool.map(run, cases), 1):
            results.append(r)
            mark = "!" if r["error"] else ("." if r["ast_match"] else "x")
            print(mark, end="", flush=True, file=sys.stderr)
            if i % 50 == 0:
                print(f" {i}", file=sys.stderr)
    print(file=sys.stderr)
    elapsed = time.time() - t0

    with out_path.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    n = len(results)
    errs = sum(1 for r in results if r["error"])
    scored = [r for r in results if not r["error"]]

    def pct(k):
        return (100.0 * sum(1 for r in scored if r[k]) / len(scored)) if scored else 0.0

    print()
    print(f"  cases            {n}   ({errs} request errors)")
    if scored:
        print(f"  compiles         {pct('compiles'):5.1f}%")
        print(f"  ast_match        {pct('ast_match'):5.1f}%   <- the number to trust")
        print(f"  exact            {pct('exact'):5.1f}%   (brittle; formatting-sensitive)")
        lat = sorted(r["latency_ms"] for r in scored)
        print(f"  latency  p50 {lat[len(lat)//2]}ms   p95 {lat[int(len(lat)*0.95)-1]}ms")
        print(f"  tokens   in {sum(r['input_tokens'] for r in scored)}"
              f"  out {sum(r['output_tokens'] for r in scored)}")
    print(f"  wall             {elapsed:.1f}s")

    # Per-template breakdown: with 10 templates, an aggregate hides which bug
    # class the model actually fails on.
    by_t = {}
    for r in scored:
        b = by_t.setdefault(r["template_id"], [0, 0, r["flags"]])
        b[0] += 1
        b[1] += bool(r["ast_match"])
    if len(by_t) > 1:
        print("\n  per template (ast_match):")
        for tid in sorted(by_t):
            tot, ok, flags = by_t[tid]
            note = f"  [{','.join(flags)}]" if flags else ""
            print(f"    template {tid:2d}  {ok:4d}/{tot:<4d} {100.0*ok/tot:5.1f}%{note}")

    fails = [r for r in scored if not r["ast_match"]][:3]
    if fails:
        print("\n  first failures:")
        for r in fails:
            print(f"    id={r['id']} template={r['template_id']} flags={r['flags']}")
            print(f"      expected: {r['expected']!r}")
            print(f"      got     : {r['got'][:160]!r}")
    if errs:
        e = next(r for r in results if r["error"])
        print(f"\n  first request error: {e['error']}")

    print(f"\n  per-case results -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
