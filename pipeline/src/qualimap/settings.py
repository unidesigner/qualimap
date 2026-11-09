"""Runtime settings, read from the environment (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_env_file(path: str | os.PathLike | None) -> None:
    """Load KEY=VALUE lines into os.environ without overriding existing values."""
    if not path:
        return
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "catalog" / "indicators.yaml").exists():
            return parent
    return Path.cwd()


@dataclass
class Settings:
    data_url: str
    pub_url: str
    public_base_url: str | None
    s3_endpoint: str | None
    s3_region: str | None
    s3_key_id: str | None
    s3_secret: str | None
    alert_webhook: str | None
    cache_dir: Path
    catalog_paths: list[Path] = field(default_factory=list)
    viewer_origins: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, catalog_paths: list[str] | None = None) -> Settings:
        env = os.environ
        root = _repo_root()
        endpoint = env.get("QM_S3_ENDPOINT")
        data_bucket = env.get("QM_BUCKET_DATA")
        pub_bucket = env.get("QM_BUCKET_PUB")
        default_local = root / ".qm"
        data_url = env.get("QM_DATA_URL") or (
            f"s3://{data_bucket}" if endpoint and data_bucket else (default_local / "data").as_uri()
        )
        pub_url = env.get("QM_PUB_URL") or (
            f"s3://{pub_bucket}" if endpoint and pub_bucket else (default_local / "pub").as_uri()
        )
        if catalog_paths:
            cats = [Path(p) for p in catalog_paths]
        else:
            cats = [root / "catalog" / "indicators.yaml"]
            local = root / "catalog" / "local"
            if local.is_dir():
                cats += sorted(local.glob("*.yaml"))
        origins = [o.strip() for o in env.get("QM_VIEWER_ORIGINS", "*").split(",") if o.strip()]
        return cls(
            data_url=data_url,
            pub_url=pub_url,
            public_base_url=env.get("QM_PUBLIC_BASE_URL"),
            s3_endpoint=endpoint,
            s3_region=env.get("QM_S3_REGION"),
            s3_key_id=env.get("QM_S3_KEY_ID"),
            s3_secret=env.get("QM_S3_SECRET"),
            alert_webhook=env.get("QM_ALERT_WEBHOOK"),
            cache_dir=Path(env.get("QM_CACHE_DIR", str(Path.home() / ".cache" / "qualimap"))),
            catalog_paths=cats,
            viewer_origins=origins,
        )

    def storage_options(self) -> dict:
        """fsspec/s3fs options for S3-compatible stores such as Backblaze B2."""
        opts: dict = {}
        if self.s3_key_id:
            opts["key"] = self.s3_key_id
        if self.s3_secret:
            opts["secret"] = self.s3_secret
        client_kwargs = {}
        if self.s3_endpoint:
            client_kwargs["endpoint_url"] = self.s3_endpoint
        if self.s3_region:
            client_kwargs["region_name"] = self.s3_region
        if client_kwargs:
            opts["client_kwargs"] = client_kwargs
        return opts
