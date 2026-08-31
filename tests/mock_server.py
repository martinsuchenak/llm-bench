#!/usr/bin/env python3
"""Mock LLM provider server for smoke-testing runner.py without real API keys.

Endpoints:
  POST /v1/messages                                (Anthropic shape)
  POST /v1beta/models/<model>:generateContent      (Google shape)
  POST /v1/chat/completions                        (OpenAI shape, also matches Z.ai)
  GET  /health

Model-name directives (substring match) inject failure modes:
  http500  -> respond 500 with a provider-shaped error body
  badjson  -> respond 200 with an unparseable body
  slow     -> sleep 30s (pair with a short task timeout to exercise timeouts)
  empty    -> respond 200 with a well-formed but text-less body

Usage: python3 tests/mock_server.py [port]
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        sys.stderr.write("[mock] " + (format % args) + "\n")

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_raw(self, status, text):
        body = text.encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": {"message": "not found: " + self.path}})

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            req = {}
        path = self.path
        model = str(req.get("model") or "")
        if not model and ":generateContent" in path:
            model = path.split("/models/")[-1].split(":")[0]

        if "http500" in model:
            self._send(500, {"type": "error",
                             "error": {"type": "api_error",
                                       "message": "mock server error (http500 directive)"}})
            return
        if "badjson" in model:
            self._send_raw(200, '{"content": [not valid json')
            return
        if "sleep35" in model:
            time.sleep(35)
        elif "slow" in model:
            time.sleep(30)
            return
        if "empty" in model:
            if path == "/v1/messages":
                self._send(200, {"id": "msg_empty", "type": "message", "role": "assistant",
                                 "model": model, "content": [],
                                 "stop_reason": "end_turn",
                                 "usage": {"input_tokens": 5, "output_tokens": 0}})
            elif ":generateContent" in path:
                self._send(200, {"candidates": [{"content": {"parts": [], "role": "model"},
                                                 "finishReason": "STOP"}],
                                 "usageMetadata": {"promptTokenCount": 5,
                                                   "candidatesTokenCount": 0}})
            else:
                self._send(200, {"id": "cc_empty", "object": "chat.completion", "model": model,
                                 "choices": [{"index": 0,
                                              "message": {"role": "assistant", "content": None},
                                              "finish_reason": "stop"}],
                                 "usage": {"prompt_tokens": 5, "completion_tokens": 0}})
            return

        text = "Mock reply from " + model

        if path == "/v1/messages":
            self._send(200, {
                "id": "msg_mock_%d" % int(time.time()),
                "type": "message", "role": "assistant", "model": model,
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 12, "output_tokens": 9},
            })
        elif path.startswith("/v1beta/models/") and path.endswith(":generateContent"):
            self._send(200, {
                "candidates": [{"content": {"parts": [{"text": text}], "role": "model"},
                                "finishReason": "STOP", "index": 0}],
                "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 9,
                                  "totalTokenCount": 21},
                "modelVersion": model,
            })
        elif path.endswith("/chat/completions"):
            self._send(200, {
                "id": "chatcmpl_mock_%d" % int(time.time()),
                "object": "chat.completion", "created": int(time.time()), "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 9},
            })
        else:
            self._send(404, {"error": {"message": "unknown path: " + path}})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("mock LLM server on http://127.0.0.1:%d" % port, flush=True)
    server.serve_forever()
