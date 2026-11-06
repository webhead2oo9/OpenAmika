"""Host/Origin check for the local services. Identical copies in brain/, speech/, messengers/.

Refuses a non-loopback Host (DNS rebinding; *.ts.net allowed for Tailscale Serve where asked) and an Origin that
is not the same host. A missing Origin passes (local programs). Not authentication.
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
    """None if allowed, else the reason."""
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
