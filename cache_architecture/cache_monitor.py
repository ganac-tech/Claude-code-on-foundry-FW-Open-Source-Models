#!/usr/bin/env python3
"""Track GLM 5.2's prompt cache for a real Claude Code fleet.

The gateway cannot tell you how much of a request was served from cache — Envoy
AI Gateway v1.0.0 reports no cached-token figure anywhere, neither in the
Anthropic response body nor on its metrics endpoint (only `input` and `output`
token types exist there). The per-turn probe in demo_cache_gateway.sh works
around that, but only because that script knows its own prefix and can replay
it. You cannot do that to Claude Code.

So measure it from the side instead: capture Claude Code's real prefix once,
then re-send it on a timer straight to Foundry and read the cache meter off the
response.

    # 1. capture the prefix Claude Code actually sends (once)
    python3 cache_architecture/cache_monitor.py capture
    ANTHROPIC_BASE_URL=http://localhost:1976/anthropic claude -p hi

    # 2. watch it
    python3 cache_architecture/cache_monitor.py watch --interval 60

What this measures is whether that prefix stays resident on the replicas your
cache key routes to. That is the thing that actually varies, and it is a
leading indicator of the fleet's hit rate — but it is not a per-user number.
A developer whose conversation has grown past the captured prefix is not
represented here.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PREFIX_FILE = REPO / "cache_architecture" / ".claude-code-prefix.json"

DIM, RED, GRN, YEL, RST = "\x1b[2m", "\x1b[31m", "\x1b[32m", "\x1b[33m", "\x1b[0m"


# ── config ──────────────────────────────────────────────────────────────────

def load_env() -> dict[str, str]:
    """Read .env the way start-gateway.sh does, with the real environment winning."""
    env: dict[str, str] = {}
    path = REPO / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
    env.update({k: v for k, v in os.environ.items() if k in env or k.startswith("FOUNDRY_")})
    for k in ("AZURE_API_KEY", "CACHE_KEY"):
        if os.environ.get(k):
            env[k] = os.environ[k]

    host = env.get("FOUNDRY_HOST", "")
    host = host.split("://", 1)[-1].split("/", 1)[0].split(":")[0]
    if not host:
        sys.exit("FOUNDRY_HOST is not set — fill in .env (see the top-level README, step 3).")
    if not env.get("AZURE_API_KEY"):
        sys.exit("AZURE_API_KEY is not set — fill in .env.")
    env["FOUNDRY_HOST"] = host
    env.setdefault("FOUNDRY_MODEL", "FW-GLM-5.2-standard")
    env.setdefault("CACHE_KEY", "claude-code-fleet")
    return env


# ── capture ─────────────────────────────────────────────────────────────────

class _Recorder(BaseHTTPRequestHandler):
    """Stands in for the gateway for exactly one request, to record what arrives.

    ThreadingHTTPServer, not HTTPServer: clients hold the connection open with
    keep-alive and a single-threaded server wedges on the second request.
    """

    captured: dict | None = None

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
        n = int(self.headers.get("content-length", 0))
        try:
            body = json.loads(self.rfile.read(n))
        except json.JSONDecodeError:
            self.send_error(400, "not JSON")
            return
        if _Recorder.captured is None and body.get("messages"):
            _Recorder.captured = body

        reply = json.dumps({
            "id": "msg_capture", "type": "message", "role": "assistant",
            "model": "cache-monitor-recorder",
            "content": [{"type": "text", "text": "captured"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *_):
        pass


def cmd_capture(args) -> int:
    server = ThreadingHTTPServer(("127.0.0.1", args.port), _Recorder)
    print(f"Recorder on http://localhost:{args.port} — waiting for one Claude Code request.\n")
    print("In another terminal:\n")
    print(f"  ANTHROPIC_BASE_URL=http://localhost:{args.port}/anthropic claude -p hi\n")
    print(f"{DIM}Ctrl-C to give up.{RST}")

    server.timeout = 0.5
    deadline = time.time() + args.timeout
    try:
        while _Recorder.captured is None and time.time() < deadline:
            server.handle_request()
    except KeyboardInterrupt:
        print("\ncancelled")
        return 1
    finally:
        server.server_close()

    if _Recorder.captured is None:
        print(f"\n{RED}Nothing captured within {args.timeout}s.{RST}")
        return 1

    body = _Recorder.captured
    # Keep only the prefix: the system prompt and tool definitions are what
    # every request repeats. The user's message is not part of what caches.
    prefix = {k: body[k] for k in ("system", "tools") if k in body}
    prefix["_captured_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    PREFIX_FILE.write_text(json.dumps(prefix, indent=2))

    approx = len(json.dumps(prefix)) // 4
    print(f"\n{GRN}Captured{RST} → {PREFIX_FILE.relative_to(REPO)}")
    print(f"  system prompt : {'yes' if 'system' in prefix else 'no'}")
    print(f"  tools         : {len(prefix.get('tools', []))}")
    print(f"  ~{approx:,} tokens of prefix\n")
    print("Now run:  python3 cache_architecture/cache_monitor.py watch")
    return 0


# ── watch ───────────────────────────────────────────────────────────────────

SYNTHETIC = (
    "You are a coding assistant operating in a terminal. "
    + ("Follow the repository's existing conventions; prefer the smallest change "
       "that fixes the problem; never invent an API you have not seen. " * 420)
)


def build_messages(prefix: dict | None) -> list[dict]:
    """OpenAI-shaped messages carrying the cacheable prefix and a trivial tail."""
    if prefix is None:
        system = SYNTHETIC
    else:
        system = prefix.get("system", "")
        if isinstance(system, list):     # Anthropic allows a block list
            system = "\n".join(b.get("text", "") for b in system)
        if prefix.get("tools"):
            # Tool definitions sit in the cached prefix too, so their bytes have
            # to be present or the measurement understates what is resident.
            system += "\n\n" + json.dumps(prefix["tools"], sort_keys=True)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "Reply with the single character: ."},
    ]


def sample(env: dict[str, str], messages: list[dict]) -> dict:
    """One 1-token request straight to Foundry. Returns a row, never raises."""
    url = f"https://{env['FOUNDRY_HOST']}/openai/v1/chat/completions"
    payload = {
        "model": env["FOUNDRY_MODEL"],
        "max_completion_tokens": 1,
        "prompt_cache_key": env["CACHE_KEY"],
        "messages": messages,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"api-key": env["AZURE_API_KEY"], "content-type": "application/json"},
    )
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "prompt_tokens": 0, "cached_tokens": 0, "pct": None,
           "latency_ms": 0, "status": "ok"}
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        # A 429 is a rate limit, not a cache miss. Counting it as one is how you
        # end up reporting a hit-rate collapse that never happened.
        row["status"] = f"http_{e.code}"
        row["latency_ms"] = int((time.time() - t0) * 1000)
        return row
    except Exception as e:  # noqa: BLE001 — a monitor must outlive any transport error
        row["status"] = type(e).__name__
        row["latency_ms"] = int((time.time() - t0) * 1000)
        return row

    u = d.get("usage", {})
    row["latency_ms"] = int((time.time() - t0) * 1000)
    row["prompt_tokens"] = u.get("prompt_tokens", 0)
    row["cached_tokens"] = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    row["pct"] = (100.0 * row["cached_tokens"] / row["prompt_tokens"]) if row["prompt_tokens"] else 0.0
    return row


def cmd_watch(args) -> int:
    env = load_env()

    prefix = None
    src = "synthetic prefix"
    path = Path(args.prefix) if args.prefix else PREFIX_FILE
    if path.exists():
        prefix = json.loads(path.read_text())
        src = f"captured prefix ({path.name}, {prefix.get('_captured_at', 'unknown date')})"
    elif args.prefix:
        sys.exit(f"{args.prefix} does not exist. Run the `capture` command first.")

    messages = build_messages(prefix)
    approx = len(json.dumps(messages)) // 4

    print(f"host      {env['FOUNDRY_HOST']}")
    print(f"model     {env['FOUNDRY_MODEL']}")
    print(f"cache key {env['CACHE_KEY']}")
    print(f"prefix    {src} · ~{approx:,} tokens")
    print(f"interval  {args.interval}s   out: {args.out}")
    if prefix is None:
        print(f"{YEL}No captured prefix — using a synthetic one. Numbers reflect a"
              f" prefix of this size,\n          not the one your fleet actually sends."
              f" Run `capture` for the real thing.{RST}")
    print()

    out = Path(args.out)
    if not out.exists():
        out.write_text("ts,prompt_tokens,cached_tokens,pct,latency_ms,status\n")

    pcts: list[float] = []
    hits = misses = errors = 0
    n = 0
    try:
        while args.samples == 0 or n < args.samples:
            n += 1
            r = sample(env, messages)
            with out.open("a") as fh:
                fh.write("{ts},{prompt_tokens},{cached_tokens},{pct},{latency_ms},{status}\n".format(
                    **{**r, "pct": "" if r["pct"] is None else f"{r['pct']:.1f}"}))

            clock = r["ts"][11:19]
            if r["status"] != "ok":
                errors += 1
                print(f"{clock}  {YEL}{r['status']}{RST}{DIM} — not counted{RST}")
            else:
                pcts.append(r["pct"])
                hit = r["pct"] >= 50
                hits, misses = hits + hit, misses + (not hit)
                badge = f"{GRN}HIT{RST}" if hit else f"{DIM}cold{RST}"
                print(f"{clock}  cached {r['cached_tokens']:>7,} / {r['prompt_tokens']:>7,}"
                      f"  ({r['pct']:5.1f}%)  {r['latency_ms']:>5,}ms  {badge}")

            if pcts and len(pcts) % 10 == 0:
                print(f"{DIM}          ── {hits}/{hits + misses} hits"
                      f" ({100.0 * hits / (hits + misses):.0f}%)"
                      f" · median {statistics.median(pcts):.0f}% of prefix cached"
                      f" · {errors} errors ──{RST}")

            if args.samples == 0 or n < args.samples:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        pass

    total = hits + misses
    print(f"\n{total} samples · {hits} hits"
          + (f" ({100.0 * hits / total:.0f}%)" if total else "")
          + (f" · median {statistics.median(pcts):.0f}% of prefix cached" if pcts else "")
          + f" · {errors} errors")
    print(f"{DIM}rows in {out}{RST}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture", help="record the prefix Claude Code sends")
    c.add_argument("--port", type=int, default=1976)
    c.add_argument("--timeout", type=int, default=300, help="seconds to wait")
    c.set_defaults(fn=cmd_capture)

    w = sub.add_parser("watch", help="sample the cache on a timer")
    w.add_argument("--interval", type=int, default=60, help="seconds between samples")
    w.add_argument("--prefix", help=f"prefix file (default: {PREFIX_FILE.name} if present)")
    w.add_argument("--out", default="cache-monitor.csv")
    w.add_argument("--samples", type=int, default=0,
                   help="stop after N samples (default 0 = run until Ctrl-C)")
    w.set_defaults(fn=cmd_watch)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
