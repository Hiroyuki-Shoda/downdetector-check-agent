"""HTTP layer.

Downdetector sits behind Cloudflare, which fingerprints the TLS/HTTP2
handshake as well as the headers. A plain ``requests`` call produces a JA3
signature no real browser would ever send and gets challenged.

So this module prefers ``curl_cffi``, which impersonates a real Chrome
handshake, and falls back to ``requests`` when it is not installed. The
fallback still works against ordinary status-page APIs; it is only
Downdetector that is likely to reject it.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

BROWSER_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
    "image/webp,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

#: Markers Cloudflare puts in its interstitial / block pages.
_CHALLENGE_MARKERS = (
    "just a moment",
    "cf-browser-verification",
    "cf_chl_opt",
    "cf-challenge",
    "attention required! | cloudflare",
    "enable javascript and cookies to continue",
    "checking your browser before accessing",
)


class FetchError(RuntimeError):
    """Raised when a URL could not be retrieved usefully."""


class BlockedError(FetchError):
    """Raised when we were served an anti-bot challenge rather than content.

    Separate from ``FetchError`` because the remedy is different: a block
    means "change how you are asking" (residential IP, real browser), not
    "retry later".
    """


@dataclass
class Response:
    url: str
    status_code: int
    text: str

    def json(self):
        import json

        return json.loads(self.text)


def _impersonating_get(url: str, *, timeout: float, headers: dict) -> Response:
    from curl_cffi import requests as cffi  # type: ignore[import-untyped]

    r = cffi.get(
        url,
        headers=headers,
        timeout=timeout,
        impersonate="chrome",
        allow_redirects=True,
    )
    return Response(url=str(r.url), status_code=r.status_code, text=r.text)


def _plain_get(url: str, *, timeout: float, headers: dict) -> Response:
    import requests

    r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    return Response(url=str(r.url), status_code=r.status_code, text=r.text)


def _has_curl_cffi() -> bool:
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        return False
    return True


def fetch(
    url: str,
    *,
    timeout: float = 20.0,
    retries: int = 2,
    headers: dict | None = None,
    accept: str | None = None,
    impersonate: bool = True,
) -> Response:
    """GET ``url`` and return the body, retrying transient failures.

    Raises ``BlockedError`` on an anti-bot challenge and ``FetchError`` on
    any other permanent failure. Callers are expected to catch both and
    degrade to ``Level.UNKNOWN`` rather than aborting the whole run.
    """
    hdrs = dict(BROWSER_HEADERS)
    if accept:
        hdrs["Accept"] = accept
    if headers:
        hdrs.update(headers)

    getter = _impersonating_get if (impersonate and _has_curl_cffi()) else _plain_get

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = getter(url, timeout=timeout, headers=hdrs)
        except Exception as exc:  # network-level failure
            last = exc
            log.debug("fetch %s attempt %d failed: %s", url, attempt + 1, exc)
        else:
            if _looks_blocked(resp):
                raise BlockedError(
                    f"anti-bot challenge from {url} (HTTP {resp.status_code}); "
                    "install curl_cffi and/or run from a non-datacenter IP"
                )
            if resp.status_code == 200:
                return resp
            # 5xx and 429 are worth retrying; other 4xx are not.
            if resp.status_code < 500 and resp.status_code != 429:
                raise FetchError(f"HTTP {resp.status_code} from {url}")
            last = FetchError(f"HTTP {resp.status_code} from {url}")

        if attempt < retries:
            # Exponential backoff with jitter, so a bank of services being
            # checked in sequence does not resynchronise onto the same retry.
            time.sleep((2**attempt) + random.uniform(0, 0.5))

    raise FetchError(f"could not fetch {url}: {last}")


def _looks_blocked(resp: Response) -> bool:
    if resp.status_code in (401, 403, 503) or resp.status_code == 429:
        lowered = resp.text[:4000].lower()
        if any(m in lowered for m in _CHALLENGE_MARKERS):
            return True
        # Cloudflare returns 403 with a tiny body for hard blocks.
        return resp.status_code == 403 and len(resp.text) < 4000
    lowered = resp.text[:4000].lower()
    return any(m in lowered for m in _CHALLENGE_MARKERS)


def fetch_json(url: str, **kw):
    """GET ``url`` expecting JSON. Status APIs never need impersonation."""
    kw.setdefault("accept", "application/json")
    kw.setdefault("impersonate", False)
    resp = fetch(url, **kw)
    try:
        return resp.json()
    except ValueError as exc:
        raise FetchError(f"invalid JSON from {url}: {exc}") from exc
