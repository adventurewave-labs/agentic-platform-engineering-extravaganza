#!/usr/bin/env python3
"""
A fake OpenAI-compatible endpoint, in-process, stdlib only.

WHY THIS EXISTS
---------------
`src/agent.py` offers `--backend llm`, and the README makes a load-bearing
claim about it: swap in a real model and the loop is unchanged, and *that it
makes no difference to the outcome is the argument*. A claim nothing exercises
is a claim that rots. Before this file, `LLMBackend.remediate` could have been
broken for months and `./run.sh verify` would still have reported all green,
because every check ran the deterministic reasoner.

So this serves the two endpoints the client actually calls -- `GET /v1/models`
for the availability probe and `POST /v1/chat/completions` for the reasoning
turn -- and replays a recorded transcript of change sets. It is deliberately
not a model:

  * No network. No API key. No token spend. Nothing to rate-limit in CI.
  * The transcript is the *deterministic reasoner's own decisions*, recorded.
    That is the honest fixture, because the argument being tested is that the
    two backends converge to the same place through the same loop.
  * The reply is wrapped in prose, because real models wrap their JSON in
    prose, and the client's extraction path has to survive that.

What this therefore tests is the plumbing the repo owns -- the wire format, the
JSON extraction, the field mapping, and convergence through the LLM code path.
It does not test whether any particular model is good at the task, which is not
a property this repository can or should assert.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

# The deterministic reasoner's decisions for the README's worked example,
# recorded turn by turn. Regenerate with:
#   PYTHONPATH=src python3 -c "..."  (see tests/test_agent.py::test_transcript_is_current)
TRANSCRIPT: list[dict] = [
    {
        "changes": {
            "db_backup_retention_days": 30,
            "db_instance_class": "db.t4g.large",
        },
        "rationale": {
            "db_backup_retention_days": "PCI requires a 30-day minimum recovery window",
            "db_instance_class": "largest reducible line item",
        },
    },
    {
        "changes": {"db_instance_class": "db.t4g.medium"},
        "rationale": {"db_instance_class": "still above the cost-centre envelope"},
    },
]


class _Handler(BaseHTTPRequestHandler):
    turns: list[dict] = []
    calls: list[str] = []

    def log_message(self, *a):  # keep CI output clean
        pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {"object": "list", "data": [
                {"id": "recorded-transcript", "object": "model"}]})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        _Handler.calls.append(request["messages"][-1]["content"])

        turn = _Handler.turns.pop(0) if _Handler.turns else {"changes": {}, "rationale": {}}
        # Prose around the JSON, the way a real model replies.
        content = (
            "Looking at the denials, the cause is in the Score parameters rather "
            "than the rendered manifests.\n\n"
            + json.dumps(turn)
            + "\n\nI have not touched any compliance control to satisfy a cost rule."
        )
        self._json(200, {
            "id": "chatcmpl-recorded",
            "object": "chat.completion",
            "model": request.get("model", "recorded-transcript"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
        })


@contextmanager
def serve(transcript: list[dict] | None = None):
    """Run the fake endpoint on an ephemeral port. Yields (base_url, handler)."""
    _Handler.turns = [dict(t) for t in (transcript if transcript is not None else TRANSCRIPT)]
    _Handler.calls = []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/v1", _Handler
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
