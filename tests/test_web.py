"""The page server must only hand out the app's own files, never secrets or stems."""
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import web  # noqa: E402


@pytest.fixture(scope="module")
def base():
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def status(url):
    opener = urllib.request.build_opener(NoRedirect)
    try:
        return opener.open(url).status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.parametrize("path", ["/practice-cover-app-v7.html", "/logo.svg", "/manifest.webmanifest", "/icons/icon-192.png"])
def test_app_files_are_served(base, path):
    assert status(base + path) == 200


@pytest.mark.parametrize("path", [
    "/passcode.txt", "/sessions.json", "/app.py", "/README.md", "/stems/", "/library/", "/tests/test_web.py",
    "/icons/../passcode.txt", "/icons/%2e%2e/passcode.txt", "/icons/", "/%2e%2e/%2e%2e/etc/passwd",
])
def test_everything_else_is_hidden(base, path):
    assert status(base + path) == 404


def test_root_redirects_to_the_page(base):
    assert status(base + "/") == 302
