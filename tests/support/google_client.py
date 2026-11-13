"""Google client test helpers: a token provider for the client."""

from __future__ import annotations


class Tokens:
    async def get(self):
        return "access"

    def invalidate(self):
        pass
