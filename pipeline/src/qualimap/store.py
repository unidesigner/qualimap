"""Object-store access (local filesystem or any S3-compatible store such as Backblaze B2).

Keys are always POSIX-style relative paths (``raw/<source>/<ts>/<file>``). The two stores
used by Qualimap are the private *data* store and the public *pub* store (SPEC §4).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import fsspec

from .settings import Settings

IMMUTABLE = "public, max-age=31536000, immutable"
SHORT = "public, max-age=300"

CONTENT_TYPES = {
    ".json": "application/json",
    ".parquet": "application/vnd.apache.parquet",
    ".tif": "image/tiff",
    ".html": "text/html",
    ".js": "text/javascript",
}


class Store:
    def __init__(self, url: str, storage_options: dict | None = None, cache_dir: Path | None = None):
        self.url = url.rstrip("/")
        opts = storage_options if url.startswith("s3://") else {}
        self.fs, root = fsspec.core.url_to_fs(self.url, **(opts or {}))
        self.root = root.rstrip("/")
        self.is_local = self.fs.protocol in ("file", "local") or "file" in (self.fs.protocol or ())
        self.cache_dir = (cache_dir or Path.home() / ".cache" / "qualimap") / hashlib.sha1(
            self.url.encode()
        ).hexdigest()[:12]
        if self.is_local:
            Path(self.root).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def path(self, key: str) -> str:
        key = key.lstrip("/")
        return f"{self.root}/{key}" if key else self.root

    def url_for(self, key: str) -> str:
        return f"{self.url}/{key.lstrip('/')}"

    # ------------------------------------------------------------------ write
    def _put_kwargs(self, key: str, cache_control: str | None) -> dict[str, Any]:
        if self.is_local:
            return {}
        kw: dict[str, Any] = {}
        ct = CONTENT_TYPES.get(Path(key).suffix)
        if ct:
            kw["ContentType"] = ct
        if cache_control:
            kw["CacheControl"] = cache_control
        return kw

    def put_bytes(self, key: str, data: bytes, cache_control: str | None = None) -> None:
        p = self.path(key)
        if self.is_local:
            Path(p).parent.mkdir(parents=True, exist_ok=True)
            Path(p).write_bytes(data)
        else:
            self.fs.pipe_file(p, data, **self._put_kwargs(key, cache_control))

    def put_json(self, key: str, obj: Any, cache_control: str | None = None) -> None:
        self.put_bytes(key, json.dumps(obj, indent=2, default=str).encode(), cache_control)

    def put_file(self, local: str | Path, key: str, cache_control: str | None = None) -> None:
        p = self.path(key)
        if self.is_local:
            Path(p).parent.mkdir(parents=True, exist_ok=True)
            if Path(local).resolve() != Path(p).resolve():
                shutil.copyfile(local, p)
        else:
            self.fs.put_file(str(local), p, **self._put_kwargs(key, cache_control))

    def put_dir(self, local_dir: str | Path, prefix: str, cache_control: str | None = None) -> int:
        local_dir = Path(local_dir)
        n = 0
        for f in sorted(local_dir.rglob("*")):
            if f.is_file():
                rel = f.relative_to(local_dir).as_posix()
                self.put_file(f, f"{prefix.rstrip('/')}/{rel}", cache_control)
                n += 1
        return n

    # ------------------------------------------------------------------ read
    def exists(self, key: str) -> bool:
        return self.fs.exists(self.path(key))

    def get_bytes(self, key: str) -> bytes:
        return self.fs.cat_file(self.path(key))

    def get_json(self, key: str, default: Any = None) -> Any:
        if not self.exists(key):
            return default
        return json.loads(self.get_bytes(key))

    def local_path(self, key: str) -> Path:
        """Path to a local copy of ``key`` (the file itself for local stores)."""
        if self.is_local:
            return Path(self.path(key))
        target = self.cache_dir / key
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            self.fs.get_file(self.path(key), str(target))
        return target

    def list(self, prefix: str = "") -> list[str]:
        """All keys below ``prefix`` (recursive), relative to the store root."""
        base = self.path(prefix)
        if not self.fs.exists(base):
            return []
        if self.fs.isfile(base):
            return [prefix]
        out = []
        for f in self.fs.find(base):
            f = f if not self.is_local else str(Path(f))
            rel = f[len(self.root) + 1 :] if f.startswith(self.root) else f
            out.append(rel.replace("\\", "/"))
        return sorted(out)

    def list_dirs(self, prefix: str) -> list[str]:
        base = self.path(prefix)
        if not self.fs.exists(base):
            return []
        return sorted(
            Path(p).name for p in self.fs.ls(base, detail=False) if self.fs.isdir(p)
        )

    def walk_keys(self, prefix: str = "") -> Iterator[str]:
        yield from self.list(prefix)

    # ------------------------------------------------------------------ delete
    def delete(self, key: str) -> None:
        p = self.path(key)
        if self.fs.exists(p):
            self.fs.rm(p, recursive=True)

    def size(self, key: str) -> int:
        return int(self.fs.size(self.path(key)))


def data_store(settings: Settings) -> Store:
    return Store(settings.data_url, settings.storage_options(), settings.cache_dir)


def pub_store(settings: Settings) -> Store:
    return Store(settings.pub_url, settings.storage_options(), settings.cache_dir)


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
