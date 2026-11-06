"""Which requests OpenAmika's local services answer. Ships as identical copies in brain/, speech/ and messengers/
(each runs in its own environment); tests/test_shipped_copies.py keeps them equal.

The services bind to 127.0.0.1, yet any web page open in the person's browser can still send requests there. Two
checks keep such pages out:
  * Host: the request must name this PC (localhost, 127.0.0.1, ::1), or for the speech gateway behind
    Tailscale_Serve_OpenAmika.bat a *.ts.net name. Any other name is DNS rebinding: a foreign site's name made to
    point at 127.0.0.1 so that the browser treats this service as that site.
  * Origin: a browser names the page a request comes from on every POST, fetch() and WebSocket. It must be this very
    host; a page of another site is refused. Requests without an Origin pass: programs on this PC (the gateway, the
    launcher) send none, and neither does a browser on a plain link, image or form-less GET, whose answer the
    foreign page cannot read. This is not authentication: anything that can reach the port may use it.
"""
from urllib.parse import urlsplit

LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
TAILNET_SUFFIX = ".ts.net"


def _hostname(netloc: str) -> str:
    try:
        return urlsplit("//" + netloc).hostname or ""
    except ValueError:
        return ""


def refusal(host, origin, allow_tailnet=False):
    """None when the request may go on, else the reason it is refused.
    host: the Host header (None when absent); origin: the Origin header (None when absent)."""
    host = (host or "").strip().lower()
    name = _hostname(host) if host else ""
    if not (name in LOOPBACK or (allow_tailnet and name.endswith(TAILNET_SUFFIX))):
        return "unknown host"
    if origin is None:
        return None
    try:
        page = urlsplit(origin.strip().lower())
        same = page.scheme in ("http", "https") and page.netloc == host
    except ValueError:
        same = False
    return None if same else "foreign origin"
