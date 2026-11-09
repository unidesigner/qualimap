from __future__ import annotations

from .base import Fetcher, FetchResult


class NoneFetcher(Fetcher):
    """Sources consumed only by the viewer (e.g. basemap styles); nothing to download."""

    def fetch(self, force: bool = False) -> FetchResult:
        return FetchResult(self.source.id, False, None, "nothing to fetch (method: none)")

    def is_due(self, now=None) -> bool:
        return False
