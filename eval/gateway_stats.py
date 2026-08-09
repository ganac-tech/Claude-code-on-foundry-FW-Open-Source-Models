#!/usr/bin/env python3
"""Cost, cache-hit rate, TTFT and latency percentiles from the local gateway.

Azure Monitor reports none of this for Fireworks models on Foundry — its cost
panel says "Cost monitoring is available for Foundry Models sold directly by
Azure only." The gateway sits in the request path and measures it all.

  python3 eval/gateway_stats.py                    # everything the gateway has seen
  python3 eval/gateway_stats.py --since 30m        # last 30 minutes
  python3 eval/gateway_stats.py --json             # machine-readable

TWO SOURCES, DIFFERENT PRECISION — the tool labels which is which:

  Envoy access log   exact per-request duration and token counts.
                     -> exact latency percentiles, exact cost, exact cache rate.
                     Flushed on a ~10s timer, so the newest request may be missing.

  aigw /metrics      Prometheus histograms. The ONLY source of time-to-first-token
                     and time-per-output-token; Envoy's access log has neither.
                     Percentiles are interpolated within histogram buckets, so
                     they are approximate — increasingly so in sparse buckets.

PRICING is a per-1M-token rate you supply. Defaults are Fireworks **direct
serverless** list rates for GLM 5.2 ($1.40 uncached input / $0.14 cached input /
$4.40 output). Foundry bills through Azure and your negotiated rate almost
certainly differs — pass --price-in/--price-cached/--price-out or set
GLM_PRICE_IN/GLM_PRICE_CACHED/GLM_PRICE_OUT. Treat the default as an estimate,
not an invoice.
"""
from __future__ import annotations  # `X | None` annotations on Python 3.9

import argparse
import json
import math
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_PRICE_IN = float(os.environ.get("GLM_PRICE_IN", "1.40"))
DEFAULT_PRICE_CACHED = float(os.environ.get("GLM_PRICE_CACHED", "0.14"))
DEFAULT_PRICE_OUT = float(os.environ.get("GLM_PRICE_OUT", "4.40"))


def find_access_log() -> Path | None:
    base = Path(os.environ.get("AIGW_STATE_HOME",
                               Path.home() / ".local/state/aigw")) / "envoy-runs"
    runs = sorted((p for p in base.glob("*/") if (p / "stdout.log").exists()),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return runs[0] / "stdout.log" if runs else None


def parse_since(s: str) -> timedelta | None:
    if not s:
        return None
    m = re.fullmatch(r"(\d+)([smhd])", s.strip())
    if not m:
        raise SystemExit(f"error: --since must look like 30m, 2h, 1d (got {s!r})")
    n, unit = int(m.group(1)), m.group(2)
    field = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[unit]
    return timedelta(**{field: n})


def load_requests(log: Path, since: timedelta | None) -> list[dict]:
    """Per-request rows from the Envoy access log."""
    cutoff = datetime.now(timezone.utc) - since if since else None
    rows = []
    for line in log.read_text(errors="replace").splitlines():
        i = line.find('{')
        if i < 0:
            continue
        try:
            d = json.loads(line[i:])
        except json.JSONDecodeError:
            continue
        if "gen_ai.request.model" not in d:
            continue
        if cutoff:
            ts = d.get("start_time")
            if ts:
                try:
                    if datetime.fromisoformat(ts.replace("Z", "+00:00")) < cutoff:
                        continue
                except ValueError:
                    pass
        rows.append(d)
    return rows


def percentile(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile on exact observations."""
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, math.ceil(q * len(sorted_vals)) - 1))
    return sorted_vals[k]


def scrape_metrics(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return parse_prom(r.read().decode())
    except Exception as e:
        print(f"warning: could not read {url} ({e}); "
              "TTFT will be unavailable", file=sys.stderr)
        return {}


def parse_prom(text: str) -> dict:
    """Collect histogram buckets keyed by metric family."""
    hists: dict[str, dict[float, float]] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        m = re.match(r"([a-z_]+)_bucket\{([^}]*)\}\s+([0-9.e+]+)", line)
        if not m:
            continue
        fam, labels, val = m.group(1), m.group(2), float(m.group(3))
        le = re.search(r'le="([^"]+)"', labels)
        if not le:
            continue
        bound = float("inf") if le.group(1) == "+Inf" else float(le.group(1))
        hists.setdefault(fam, {})
        hists[fam][bound] = hists[fam].get(bound, 0.0) + val
    return hists


def hist_percentile(buckets: dict[float, float], q: float) -> float | None:
    """Interpolate a percentile from cumulative histogram buckets.

    Approximate by construction: within a bucket we can only assume a uniform
    distribution. Fine for a p50/p95 sanity read, not for SLA arithmetic.
    """
    if not buckets:
        return None
    bounds = sorted(buckets)
    total = buckets[bounds[-1]]
    if total <= 0:
        return None
    target = q * total
    prev_bound, prev_count = 0.0, 0.0
    for b in bounds:
        c = buckets[b]
        if c >= target:
            if math.isinf(b):
                return prev_bound
            if c == prev_count:
                return b
            frac = (target - prev_count) / (c - prev_count)
            return prev_bound + frac * (b - prev_bound)
        prev_bound, prev_count = b, c
    return bounds[-2] if len(bounds) > 1 else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default=None, help="Envoy access log (default: newest run)")
    ap.add_argument("--metrics", default="http://localhost:1064/metrics")
    ap.add_argument("--since", default="", help="30m, 2h, 1d — access log only")
    ap.add_argument("--price-in", type=float, default=DEFAULT_PRICE_IN,
                    help="$ per 1M uncached input tokens")
    ap.add_argument("--price-cached", type=float, default=DEFAULT_PRICE_CACHED,
                    help="$ per 1M cached input tokens")
    ap.add_argument("--price-out", type=float, default=DEFAULT_PRICE_OUT,
                    help="$ per 1M output tokens")
    ap.add_argument("--assume-cached-pct", type=float, default=None,
                    help="model cost as if N%% of input tokens were cache hits. "
                         "Use when the gateway reports 0 cached tokens but you "
                         "know the upstream is caching (see the warning below).")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    log = Path(args.log) if args.log else find_access_log()
    if not log or not log.exists():
        print("error: no Envoy access log found. Is the gateway running?",
              file=sys.stderr)
        return 1

    rows = load_requests(log, parse_since(args.since))
    hists = scrape_metrics(args.metrics)

    n = len(rows)
    if not n:
        print(f"No requests found in {log}"
              + (f" within --since {args.since}" if args.since else ""),
              file=sys.stderr)
        return 1

    ok = [r for r in rows if str(r.get("response_code")) == "200"]
    errors = n - len(ok)

    def num(r, k):
        v = r.get(k)
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    tin = sum(num(r, "gen_ai.usage.input_tokens") for r in ok)
    tout = sum(num(r, "gen_ai.usage.output_tokens") for r in ok)
    tcached = sum(num(r, "gen_ai.usage.cached_input_tokens") for r in ok)
    tcreate = sum(num(r, "gen_ai.usage.cache_creation_input_tokens") for r in ok)

    # Cached tokens are reported inside input_tokens by the OpenAI schema, so
    # bill the uncached remainder at full rate and the cached part at the
    # cached rate. Clamp: a mismatched provider could report cached > input.
    uncached = max(0, tin - tcached)
    cost_in = uncached / 1e6 * args.price_in
    cost_cached = tcached / 1e6 * args.price_cached
    cost_out = tout / 1e6 * args.price_out
    cost = cost_in + cost_cached + cost_out

    lat = sorted(float(num(r, "duration_ms")) for r in ok)
    ttft = hists.get("gen_ai_server_time_to_first_token_seconds", {})
    tpot = hists.get("gen_ai_server_time_per_output_token_seconds", {})
    dur_h = hists.get("gen_ai_server_request_duration_seconds", {})

    models = sorted({r.get("gen_ai.response.model", "?") for r in ok})

    out = {
        "requests": n, "ok": len(ok), "errors": errors,
        "models": models,
        "tokens": {"input": tin, "uncached_input": uncached, "cached_input": tcached,
                   "cache_creation_input": tcreate, "output": tout},
        "cache_hit_rate_pct": (100.0 * tcached / tin) if tin else 0.0,
        # True when the gateway reported no cache hits at all. Upstream caching
        # may still be happening — aigw v1.0.0 drops the field in translation —
        # so cost_usd.total is an upper bound whenever this is set.
        "cache_reporting_unavailable": bool(tin and not tcached),
        "cost_usd": {"input": cost_in, "cached_input": cost_cached,
                     "output": cost_out, "total": cost,
                     "per_request": cost / len(ok) if ok else 0.0},
        "prices_per_1m": {"input": args.price_in, "cached_input": args.price_cached,
                          "output": args.price_out},
        "latency_ms_exact": {f"p{int(q*100)}": percentile(lat, q)
                             for q in (0.5, 0.9, 0.95, 0.99)},
        "ttft_ms_approx": {f"p{int(q*100)}": (lambda v: round(v * 1000, 1) if v else None)(
            hist_percentile(ttft, q)) for q in (0.5, 0.9, 0.95, 0.99)},
        "tpot_ms_approx": {f"p{int(q*100)}": (lambda v: round(v * 1000, 1) if v else None)(
            hist_percentile(tpot, q)) for q in (0.5, 0.9, 0.95)},
        "source": {"access_log": str(log), "metrics": args.metrics},
    }

    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    W = 62
    print("═" * W)
    print(f" Gateway stats{('  (last ' + args.since + ')') if args.since else ''}")
    print("═" * W)
    print(f"  requests           {n}   ok {len(ok)}   errors {errors}")
    print(f"  served by          {', '.join(models)}")
    print()
    print("  ── latency ─ exact, from Envoy access log " + "─" * 18)
    for q in ("p50", "p90", "p95", "p99"):
        print(f"    total {q:<4}       {out['latency_ms_exact'][q]:>9,.0f} ms")
    print()
    print("  ── time to first token ─ approx, histogram-interpolated ───")
    if ttft:
        for q in ("p50", "p90", "p95", "p99"):
            v = out["ttft_ms_approx"][q]
            print(f"    TTFT {q:<4}        {v:>9,.1f} ms" if v is not None
                  else f"    TTFT {q:<4}        {'n/a':>9}")
        if tpot:
            print()
            for q in ("p50", "p90", "p95"):
                v = out["tpot_ms_approx"][q]
                if v is not None:
                    tps = 1000.0 / v if v else 0
                    print(f"    per-output-token {q:<4} {v:>7,.1f} ms  (~{tps:,.0f} tok/s)")
    else:
        print("    no data — TTFT is only recorded for STREAMING requests")
        print("    (\"stream\": true). Non-streaming traffic has no first-token")
        print("    event to measure. Claude Code always streams, so this fills")
        print("    in as soon as real traffic flows. Also check --metrics if the")
        print("    gateway was restarted — the counters reset with the process.")
    print()
    print("  ── prompt cache " + "─" * 42)
    print(f"    input tokens          {tin:>12,}")
    print(f"      cached (hit)        {tcached:>12,}   {out['cache_hit_rate_pct']:5.1f}% hit rate")
    print(f"      uncached            {uncached:>12,}")
    if tcreate:
        print(f"    cache writes          {tcreate:>12,}")
    print(f"    output tokens         {tout:>12,}")
    print()
    print("  ── cost ─ ESTIMATE at the rates below " + "─" * 20)
    print(f"    uncached in  {uncached:>10,} @ ${args.price_in:>6.2f}/M   ${cost_in:>9.4f}")
    print(f"    cached in    {tcached:>10,} @ ${args.price_cached:>6.2f}/M   ${cost_cached:>9.4f}")
    print(f"    output       {tout:>10,} @ ${args.price_out:>6.2f}/M   ${cost_out:>9.4f}")
    print(f"    {'total':<38} ${cost:>9.4f}")
    print(f"    {'per request':<38} ${out['cost_usd']['per_request']:>9.4f}")
    if tcached:
        saved = tcached / 1e6 * (args.price_in - args.price_cached)
        print(f"    {'saved by cache vs all-uncached':<38} ${saved:>9.4f}")

    if args.assume_cached_pct is not None:
        pct = max(0.0, min(100.0, args.assume_cached_pct))
        hit = int(tin * pct / 100.0)
        alt = ((tin - hit) / 1e6 * args.price_in
               + hit / 1e6 * args.price_cached
               + cost_out)
        print()
        print(f"    modelled at {pct:.0f}% cache hit:       ${alt:>9.4f}"
              f"   ({100.0 * (1 - alt / cost) if cost else 0:.0f}% lower)")

    if tin and not tcached:
        print()
        print("  ⚠ COST ABOVE IS AN UPPER BOUND — cache hits are unreported.")
        print("    Envoy AI Gateway v1.0.0 does not map the upstream OpenAI field")
        print("    `usage.prompt_tokens_details.cached_tokens` into the Anthropic")
        print("    `cache_read_input_tokens` it returns, so every request looks")
        print("    100% uncached here even when Foundry served it from cache.")
        print("    Verified on this deployment: an identical 5,616-token prefix")
        print("    reported cached_tokens=0 through the gateway and 5,613 when")
        print("    queried directly. Every cached token is being billed at the")
        print(f"    uncached rate (${args.price_in:.2f}/M vs ${args.price_cached:.2f}/M) in this total.")
        print("    Re-run with --assume-cached-pct to model the real figure, and")
        print("    reconcile against the Azure invoice for the authoritative number.")

    print()
    print("  Rates are Fireworks direct-serverless list prices unless you passed")
    print("  --price-*. Foundry bills through Azure at your negotiated rate, so")
    print("  treat the total as an estimate. Reconcile against the Azure invoice.")
    print("═" * W)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
