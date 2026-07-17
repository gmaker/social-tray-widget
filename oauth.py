"""Shared OAuth helper: a one-shot localhost server that captures the redirect.

Every provider builds its own authorize URL and exchanges the code itself —
only the "open a browser, catch the ?code=... redirect" part is common, so it
lives here. The server handles exactly one request and shuts down.
"""

from __future__ import annotations

import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional


_SUCCESS = (b"<html><body style='font-family:sans-serif;background:#141414;"
            b"color:#eee;text-align:center;padding-top:20vh'>"
            b"<h1>Authorised &mdash; you can close this tab.</h1></body></html>")
_FAILURE = (b"<html><body style='font-family:sans-serif;background:#141414;"
            b"color:#eee;text-align:center;padding-top:20vh'>"
            b"<h1>Auth failed &mdash; close this tab and retry.</h1></body></html>")


class LoopbackCapture:
    """Open `auth_url`, block until the provider redirects back, return its query
    params as a flat dict (e.g. {"code": "...", "state": "..."})."""

    def __init__(self, redirect_uri: str):
        self.redirect_uri = redirect_uri
        self.params: Optional[dict] = None

    def capture(self, auth_url: str) -> dict:
        parsed = urllib.parse.urlparse(self.redirect_uri)
        port   = parsed.port or 8080
        cap    = self

        class _H(BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                cap.params = {k: v[0] for k, v in q.items()}
                ok = "code" in cap.params
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_SUCCESS if ok else _FAILURE)
                threading.Thread(target=srv.shutdown, daemon=True).start()

            def log_message(self, *_):
                pass

        srv = HTTPServer(("localhost", port), _H)
        webbrowser.open(auth_url)
        try:
            srv.serve_forever()
        finally:
            srv.server_close()
        return cap.params or {}
