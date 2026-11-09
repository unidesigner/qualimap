from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from ..catalog import Catalog, Source, unresolved
from ..state import FetchRecord, StateStore, ts_compact, utcnow
from ..store import Store, sha256_file

USER_AGENT = "qualimap-pipeline/0.1 (+https://github.com/unidesigner/qualimap)"


@dataclass
class FetchContext:
    catalog: Catalog
    store: Store
    state: StateStore
    client: httpx.Client | None = None
    workdir: Path = Path("/tmp/qualimap-fetch")
    retries: int = 3
    backoff_s: float = 2.0

    def http(self) -> httpx.Client:
        if self.client is None:
            self.client = httpx.Client(
                headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=120.0
            )
        return self.client


@dataclass
class FetchResult:
    source_id: str
    changed: bool
    record: FetchRecord | None
    message: str = ""
    files: list[str] = field(default_factory=list)


class Fetcher:
    """Base class. Subclasses implement :meth:`download` returning local files + metadata."""

    def __init__(self, source: Source, ctx: FetchContext):
        self.source = source
        self.ctx = ctx

    # -------------------------------------------------------------- policy
    def is_due(self, now: datetime | None = None) -> bool:
        rec = self.ctx.state.fetch_record(self.source.id)
        if rec is None or rec.status == "failed":
            return True
        last = datetime.fromisoformat(rec.fetched_at)
        return (now or utcnow()) - last >= self.source.refresh_every

    def check_config(self) -> None:
        missing = unresolved(self.source.fetch)
        if missing:
            raise RuntimeError(
                f"{self.source.id}: unresolved environment variables {sorted(set(missing))}"
            )

    # -------------------------------------------------------------- template
    def fetch(self, force: bool = False) -> FetchResult:
        self.check_config()
        prev = self.ctx.state.fetch_record(self.source.id)
        last_err: Exception | None = None
        for attempt in range(self.ctx.retries):
            try:
                return self._fetch_once(prev, force)
            except (httpx.HTTPError, OSError) as e:
                last_err = e
                time.sleep(self.ctx.backoff_s * (2**attempt))
        if prev:
            self.ctx.state.mark_source(self.source.id, "stale", str(last_err))
        raise RuntimeError(f"{self.source.id}: fetch failed after retries: {last_err}")

    def _fetch_once(self, prev: FetchRecord | None, force: bool) -> FetchResult:
        workdir = self.ctx.workdir / self.source.id / ts_compact()
        workdir.mkdir(parents=True, exist_ok=True)
        outcome = self.download(workdir, prev, force)
        if outcome is None:  # upstream unchanged (ETag / checksum)
            return FetchResult(self.source.id, False, prev, "unchanged upstream")
        files, meta = outcome
        digest = _combined_sha([sha256_file(f) for f, _ in files])
        if prev and not force and self.source.refresh_check != "always" and digest == prev.sha256:
            return FetchResult(self.source.id, False, prev, "unchanged content")
        stamp = ts_compact()
        entries = []
        for local, url in files:
            key = f"raw/{self.source.id}/{stamp}/{Path(local).name}"
            self.ctx.store.put_file(local, key)
            entries.append(
                {"key": key, "name": Path(local).name, "bytes": Path(local).stat().st_size,
                 "sha256": sha256_file(local), "url": url}
            )
        rec = FetchRecord(
            source_id=self.source.id,
            fetched_at=utcnow().isoformat(),
            sha256=digest,
            files=entries,
            etag=meta.get("etag"),
            last_modified=meta.get("last_modified"),
            upstream=meta.get("upstream", {}),
        )
        self.ctx.state.save_fetch_record(rec)
        return FetchResult(self.source.id, True, rec, f"{len(entries)} file(s)", [e["key"] for e in entries])

    def download(
        self, workdir: Path, prev: FetchRecord | None, force: bool
    ) -> tuple[list[tuple[Path, str]], dict] | None:
        raise NotImplementedError

    # -------------------------------------------------------------- helpers
    def stream_to(self, url: str, dest: Path, headers: dict | None = None) -> httpx.Response:
        with self.ctx.http().stream("GET", url, headers=headers or {}) as r:
            if r.status_code == 304:
                return r
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
            return r


def _combined_sha(shas: list[str]) -> str:
    import hashlib

    return hashlib.sha256("".join(sorted(shas)).encode()).hexdigest() if len(shas) > 1 else shas[0]


def filename_from(url: str, response: httpx.Response | None = None, default: str = "download") -> str:
    if response is not None:
        cd = response.headers.get("content-disposition", "")
        if "filename=" in cd:
            return cd.split("filename=")[-1].strip('"; ')
    name = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    return name or default
