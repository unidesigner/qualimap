from __future__ import annotations

from pathlib import Path

from ..state import FetchRecord
from .base import Fetcher, filename_from


class HttpFetcher(Fetcher):
    """Plain HTTP(S) GET. With ``refresh.check: etag`` a conditional request skips unchanged files."""

    def download(self, workdir: Path, prev: FetchRecord | None, force: bool):
        url = self.source.fetch["url"]
        headers = {}
        if prev and not force and self.source.refresh_check == "etag":
            if prev.etag:
                headers["If-None-Match"] = prev.etag
            if prev.last_modified:
                headers["If-Modified-Since"] = prev.last_modified
        tmp = workdir / "download.part"
        r = self.stream_to(url, tmp, headers)
        if r.status_code == 304:
            return None
        name = self.source.fetch.get("filename") or filename_from(url, r)
        dest = workdir / name
        tmp.rename(dest)
        meta = {"etag": r.headers.get("etag"), "last_modified": r.headers.get("last-modified")}
        return [(dest, url)], meta


class OsmPbfFetcher(HttpFetcher):
    """Geofabrik extracts: HTTP with ETag (SPEC: refresh P7D, check etag)."""


class GtfsFetcher(HttpFetcher):
    """GTFS ZIP feeds over HTTP."""
