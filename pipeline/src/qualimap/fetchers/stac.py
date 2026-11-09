from __future__ import annotations

import fnmatch
from pathlib import Path

from ..state import FetchRecord
from .base import Fetcher, filename_from

DEFAULT_STAC_ROOT = "https://data.geo.admin.ch/api/stac/v0.9"


class StacFetcher(Fetcher):
    """Download assets of a STAC collection (geo.admin.ch STAC API by default).

    Selects assets by ``asset_media_type`` (and optional ``asset_pattern`` glob on the href).
    Without ``per_tile`` only the most recently updated item is used. Unchanged upstream is
    detected from the asset checksums (``file:checksum`` / ``checksum:multihash``) or, when the
    API publishes none, from the ``updated`` timestamps.
    """

    def items(self) -> list[dict]:
        f = self.source.fetch
        root = f.get("stac_root", DEFAULT_STAC_ROOT).rstrip("/")
        url = f"{root}/collections/{f['collection']}/items"
        params: dict = {"limit": 100}
        if f.get("bbox"):
            params["bbox"] = ",".join(str(v) for v in f["bbox"])
        out: list[dict] = []
        while url:
            r = self.ctx.http().get(url, params=params)
            r.raise_for_status()
            page = r.json()
            out.extend(page.get("features", []))
            url = next((lnk["href"] for lnk in page.get("links", []) if lnk.get("rel") == "next"), None)
            params = {}
        return out

    def select_assets(self, items: list[dict]) -> list[dict]:
        f = self.source.fetch
        media = f.get("asset_media_type")
        pattern = f.get("asset_pattern")
        if not f.get("per_tile") and items:
            items = [max(items, key=lambda it: it.get("properties", {}).get("updated")
                         or it.get("properties", {}).get("datetime") or "")]
        chosen = []
        for it in items:
            for name, a in (it.get("assets") or {}).items():
                if media and not (a.get("type", "").split(";")[0].strip() == media
                                  or a.get("type", "").startswith(media)):
                    continue
                if pattern and not fnmatch.fnmatch(a.get("href", ""), pattern):
                    continue
                chosen.append({"item": it.get("id"), "name": name, **a})
        return chosen

    def download(self, workdir: Path, prev: FetchRecord | None, force: bool):
        assets = self.select_assets(self.items())
        if not assets:
            raise RuntimeError(f"{self.source.id}: no STAC assets match {self.source.fetch}")
        upstream = {
            a["href"]: a.get("file:checksum") or a.get("checksum:multihash") or a.get("updated") or ""
            for a in assets
        }
        if prev and not force and prev.upstream == upstream and all(upstream.values()):
            return None
        files = []
        for a in assets:
            dest = workdir / filename_from(a["href"])
            self.stream_to(a["href"], dest)
            files.append((dest, a["href"]))
        return files, {"upstream": upstream}
