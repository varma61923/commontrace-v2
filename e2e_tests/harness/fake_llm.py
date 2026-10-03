from __future__ import annotations

import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@contextlib.contextmanager
def fake_model(reply: dict):
    """A local OpenAI-compatible chat endpoint that always answers with *reply* as JSON."""
    prompts: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            prompts.append(body["messages"][-1]["content"])
            payload = json.dumps({
                "choices": [{"message": {"role": "assistant", "content": json.dumps(reply)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {
        "COMMONTRACE_LLM_PROVIDER": "openai-compatible",
        "COMMONTRACE_LLM_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}",
        "COMMONTRACE_LLM_API_KEY": "test-key",
        "COMMONTRACE_LLM_MODEL": "fake",
    }
    try:
        yield env, prompts
    finally:
        server.shutdown()
        server.server_close()
