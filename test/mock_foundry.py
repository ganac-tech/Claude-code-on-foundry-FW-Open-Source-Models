#!/usr/bin/env python3
"""Mock Microsoft Foundry OpenAI chat-completions endpoint.

Stands in for https://<resource>.services.ai.azure.com/openai/v1/chat/completions
so the Anthropic -> OpenAI translation can be verified with no Azure credentials
and no network. Records every upstream request it receives to
test/upstream-requests.jsonl for assertion.

  python3 test/mock_foundry.py [port]     # default 8931
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RECORD = Path(__file__).parent / "upstream-requests.jsonl"
EXPECTED_PATH = "/openai/v1/chat/completions"


def _chat_completion(tool_call: bool):
    """A minimal but well-formed OpenAI chat.completion response."""
    if tool_call:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_mock_0",
                "type": "function",
                "function": {"name": "get_weather",
                             "arguments": '{"city": "Paris"}'},
            }],
        }
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": "pong"}
        finish = "stop"
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": 0,
        "model": "FW-GLM-5.2",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
    }


def _sse_chunks():
    """OpenAI streaming chunks; the gateway converts these to Anthropic SSE."""
    base = {"id": "chatcmpl-mock", "object": "chat.completion.chunk",
            "created": 0, "model": "FW-GLM-5.2"}
    yield {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                                "finish_reason": None}]}
    for piece in ("one ", "two ", "three"):
        yield {**base, "choices": [{"index": 0, "delta": {"content": piece},
                                    "finish_reason": None}]}
    yield {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
           "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("mock-foundry: " + fmt % args + "\n")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {"_unparseable": raw.decode("utf-8", "replace")}

        # Record exactly what the gateway sent upstream.
        with RECORD.open("a") as fh:
            fh.write(json.dumps({
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }) + "\n")

        if self.path.split("?")[0] != EXPECTED_PATH:
            return self._json(404, {"error": {"message": f"unexpected path {self.path}"}})

        if body.get("stream"):
            return self._stream()
        wants_tool = bool(body.get("tools"))
        self._json(200, _chat_completion(tool_call=wants_tool))

    def _json(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for chunk in _sse_chunks():
            self._chunk(f"data: {json.dumps(chunk)}\n\n".encode())
        self._chunk(b"data: [DONE]\n\n")
        self._chunk(b"")

    def _chunk(self, payload: bytes):
        self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
        self.wfile.flush()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8931
    RECORD.unlink(missing_ok=True)
    print(f"mock foundry on :{port}, recording to {RECORD}", flush=True)
    # Must be threaded: Envoy holds HTTP/1.1 keep-alive connections open, which
    # would block a single-threaded HTTPServer from ever accepting another.
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
