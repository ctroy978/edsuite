#!/usr/bin/env python
"""Minimal local stub for the xAI cleanup endpoint."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

def extract_text_from_messages(messages: list[dict]) -> str:
    if len(messages) < 2:
        return ""
    content = messages[1].get("content", "")
    if "<BEGIN_TEXT>" in content and "<END_TEXT>" in content:
        snippet = content.split("<BEGIN_TEXT>", 1)[1]
        snippet = snippet.split("<END_TEXT>", 1)[0]
        return snippet.strip()
    return str(content).strip()


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json_response(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            request = json.loads(body)
            cleaned = extract_text_from_messages(request.get("messages") or [])
        except json.JSONDecodeError:
            self._json_response({"error": "invalid json"}, status=400)
            return

        restored = cleaned.replace("[[UNK]]", "(restored)")
        payload = {"choices": [{"message": {"content": restored}}]}
        self._json_response(payload)

    def log_message(self, format: str, *args) -> None:  # noqa: A003 (match parent signature)
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a mock xAI cleanup server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8135)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    server = HTTPServer((args.host, args.port), MockHandler)
    print(f"[mock_xai] Listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
