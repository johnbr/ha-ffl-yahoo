"""Small async HTTP fakes for the transport and config-flow contract tests."""

from __future__ import annotations


class HttpFailure(Exception):
    """HTTP error raised by a response's raise_for_status."""


class Response:
    def __init__(self, status=200, body="{}", headers=None, chunk_size=4096):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.content = self
        self.remaining = body.encode()
        self.chunk_size = chunk_size
        self.read_bytes = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def read(self, n):
        chunk, self.remaining = self.remaining[: min(n, self.chunk_size)], self.remaining[min(n, self.chunk_size) :]
        self.read_bytes += len(chunk)
        return chunk

    async def text(self):
        return self.body

    def raise_for_status(self):
        if self.status >= 400:
            raise HttpFailure(f"HTTP {self.status}")


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response
