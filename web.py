"""Tiny static file server for the app page. Unlike `python -m http.server` it only serves the files the
page needs, so passcode.txt, sessions.json, stems/ and library/ are never reachable on this port.

Usage: python web.py [port]      (default 5500)
"""
import http.server
import posixpath
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent
PAGE = "/practice-cover-app-v7.html"
ALLOWED = {PAGE, "/logo.svg", "/manifest.webmanifest"}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def _allowed(self) -> bool:
        raw = self.path.split("?", 1)[0].split("#", 1)[0]
        path = posixpath.normpath(unquote(raw))
        return path in ALLOWED or (path.startswith("/icons/") and path.count("/") == 2)

    def _gate(self) -> bool:
        raw = self.path.split("?", 1)[0]
        if raw in ("", "/"):
            self.send_response(302)
            self.send_header("Location", PAGE)
            self.end_headers()
            return False
        if not self._allowed():
            self.send_error(404)
            return False
        return True

    def do_GET(self):
        if self._gate():
            super().do_GET()

    def do_HEAD(self):
        if self._gate():
            super().do_HEAD()

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")  # always pick up app updates
        super().end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5500
    with http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler) as server:
        print(f"Serving the app on http://0.0.0.0:{port}{PAGE}", flush=True)
        server.serve_forever()
