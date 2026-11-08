"""Shared HTTP boundary for setup, live polling and player history.

Keep Yahoo's access refusals distinct from transport failures. Never include an
arbitrary upstream response body (which could contain session data) in errors.
This module does not acquire cookies, retry requests or implement authentication.

A private league's seed is served only to a signed-in member, so the user may
supply their own Yahoo browser cookie. It is sent to :data:`COOKIE_HOST` alone —
never to the relay, ESPN or anywhere else — and is never logged.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from .web_client import YahooWebError


class YahooAccessDenied(YahooWebError):
    """Yahoo returned a missing-league response or a generic access refusal."""

    def __init__(self, status: int, *, league_not_found: bool = False) -> None:
        self.status = status
        self.league_not_found = league_not_found
        if league_not_found:
            detail = "Yahoo has no NFL league with that id this season. Check the number after /f1/ in your league address."
        else:
            detail = "Yahoo refused access to its league data."
        super().__init__(f"Yahoo request failed (HTTP {status}). {detail}")


def league_refused(err: BaseException | None) -> bool:
    """Whether Yahoo answered the league seed with its "no league" refusal.

    Looks through the cause chain, because a refusal that outlasts the stale
    window arrives wrapped in the ``FetchFailed`` that ends it.
    """
    while err is not None:
        if isinstance(err, YahooAccessDenied) and err.league_not_found:
            return True
        err = err.__cause__
    return False


class YahooRefused(YahooWebError):
    """Yahoo is turning this host away: HTTP 999 ("Request denied") or 429.

    Not an answer about the league, and not a transport failure to retry on
    the next poll: it says the host has been asking too often, and the
    caller's job is to stop asking for a while (see ``redzone_client``).
    """

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Yahoo is refusing requests from this host (HTTP {status}).")


# The league seed's host: the only one a user's Yahoo cookie is ever sent to.
COOKIE_HOST = "pub-api.fantasysports.yahoo.com"


def normalize_cookie(raw: str) -> str:
    """The cookie as an HTTP header value, from whatever the user pasted.

    Accepts the bare value or a whole ``Cookie: ...`` line, and joins a paste
    that wrapped across lines. Raises ``ValueError`` if it cannot be one.
    """
    text = " ".join(str(raw or "").split())
    if text[:7].lower() == "cookie:":
        text = text[7:].strip()
    if not text:
        return ""
    if "=" not in text or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError("not a cookie header value")
    return text


def _sends_cookie(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == COOKIE_HOST


# 999 is Yahoo's own "Request denied", served to a host it has throttled.
REFUSED_STATUSES = frozenset({429, 999})


def _league_not_found(body: str) -> bool:
    """Recognize the known error without echoing arbitrary server text."""
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
        return False
    return payload["error"].get("description") == "Unable to retrieve cookie."


async def fetch_text(
    session: Any,
    url: str,
    *,
    headers: dict[str, str],
    timeout: Any = None,
    allow_not_modified: bool = False,
    cookie: str = "",
) -> tuple[str | None, str]:
    """Read text and Last-Modified, preserving status-based access failures.

    Other HTTP errors retain the session's usual exception and are handled by
    the existing transient-error/backoff code. A 304 is accepted only for the
    caller that explicitly requests conditional responses. ``cookie`` is added
    only when ``url`` is on :data:`COOKIE_HOST`.
    """
    if cookie and _sends_cookie(url):
        headers = {**headers, "Cookie": cookie}
    kwargs: dict[str, Any] = {"headers": headers}
    if timeout is not None:
        kwargs["timeout"] = timeout
    async with session.get(url, **kwargs) as response:
        if response.status in REFUSED_STATUSES:
            raise YahooRefused(response.status)
        if response.status in (401, 403):
            # Bound the read: a denied request may serve a large HTML page.
            chunks = bytearray()
            while len(chunks) < 4096:
                chunk = await response.content.read(4096 - len(chunks))
                if not chunk:
                    break
                chunks.extend(chunk)
            body = chunks.decode("utf-8", errors="replace")
            raise YahooAccessDenied(response.status, league_not_found=response.status == 403 and _league_not_found(body))
        if response.status == 304 and allow_not_modified:
            return None, ""
        response.raise_for_status()
        return await response.text(), response.headers.get("Last-Modified", "")
