"""Release versioning, publication, rollback and diff (SPEC §4.2, §8.3)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from .state import utcnow
from .store import IMMUTABLE, SHORT, Store


def list_releases(pub: Store) -> list[dict]:
    out = []
    for v in pub.list_dirs("releases"):
        m = pub.get_json(f"releases/{v}/manifest.json")
        if m:
            size = sum(f["bytes"] for f in m.get("files", []))
            out.append({"version": v, "created_at": m.get("created_at"), "bytes": size,
                        "indicators": sorted(m.get("indicators", {})), "git_sha": m.get("git_sha")})
    return sorted(out, key=lambda r: _vkey(r["version"]))


def latest(pub: Store) -> dict | None:
    return pub.get_json("releases/latest.json")


def _vkey(v: str) -> tuple:
    try:
        return tuple(int(p) for p in v.split("."))
    except ValueError:
        return (0, 0, 0)


def next_version(pub: Store, now=None) -> str:
    now = now or utcnow()
    prefix = f"{now.year}.{now.month:02d}"
    nums = [int(v.split(".")[2]) for v in pub.list_dirs("releases") if v.startswith(prefix + ".")]
    return f"{prefix}.{max(nums, default=0) + 1}"


def publish(pub: Store, local_dir: Path, version: str, switch: bool = True,
            public_base_url: str | None = None) -> dict:
    if pub.exists(f"releases/{version}/manifest.json"):
        raise RuntimeError(f"release {version} already exists (releases are immutable)")
    n = 0
    manifest_path = local_dir / "manifest.json"
    for f in sorted(local_dir.rglob("*")):
        if f.is_file() and f != manifest_path:
            pub.put_file(f, f"releases/{version}/{f.relative_to(local_dir).as_posix()}", IMMUTABLE)
            n += 1
    # manifest last: a release is complete once its manifest exists
    pub.put_file(manifest_path, f"releases/{version}/manifest.json", IMMUTABLE)
    if switch:
        switch_latest(pub, version, public_base_url)
    return {"version": version, "files": n + 1, "switched": switch}


def switch_latest(pub: Store, version: str, public_base_url: str | None = None) -> dict:
    if not pub.exists(f"releases/{version}/manifest.json"):
        raise RuntimeError(f"release {version} not found")
    base = (public_base_url.rstrip("/") + "/") if public_base_url else ""
    doc = {"version": version, "manifest": f"{base}releases/{version}/manifest.json",
           "catalog": f"{base}releases/{version}/catalog.json", "base": f"{base}releases/{version}/",
           "switched_at": utcnow().isoformat()}
    pub.put_json("releases/latest.json", doc, SHORT)
    return doc


def _cells(pub: Store, version: str, res: int) -> pd.DataFrame:
    return pd.read_parquet(pub.local_path(f"releases/{version}/parquet/cells_r{res}.parquet"))


def diff(pub: Store, a: str, b: str, res: int = 13, commune: int | None = None) -> pd.DataFrame:
    """Per-indicator change statistics between two releases at ``res`` (scores 0–10)."""
    da, db = _cells(pub, a, res), _cells(pub, b, res)
    if commune is not None:
        da, db = da[da["commune_bfs"] == commune], db[db["commune_bfs"] == commune]
    m = da.merge(db, on="cell_id", suffixes=("_a", "_b"))
    rows = []
    for col in sorted(c for c in da.columns if c.startswith("q_")):
        if f"{col}_b" not in m:
            rows.append({"indicator": col[2:], "status": "removed"})
            continue
        qa_, qb = m[f"{col}_a"].to_numpy(), m[f"{col}_b"].to_numpy()
        ok = (qa_ != 255) & (qb != 255)
        d = (qb[ok].astype(float) - qa_[ok].astype(float)) / 25.0
        rows.append({"indicator": col[2:], "status": "ok", "cells": int(ok.sum()),
                     "mean_shift": float(d.mean()) if len(d) else 0.0,
                     "mean_abs_shift": float(np.abs(d).mean()) if len(d) else 0.0,
                     "changed_share": float((np.abs(d) >= 0.5).mean()) if len(d) else 0.0,
                     "nodata_a": float((qa_ == 255).mean()), "nodata_b": float((qb == 255).mean())})
    for col in sorted(c for c in db.columns if c.startswith("q_") and c not in da.columns):
        rows.append({"indicator": col[2:], "status": "added"})
    return pd.DataFrame(rows)


def diff_local(pub: Store, previous: str | None, new_dir: Path, res: int = 13) -> pd.DataFrame | None:
    """Diff a not-yet-published release directory against a published one."""
    if not previous:
        return None
    with tempfile.TemporaryDirectory() as d:
        tmp = Store(Path(d).as_uri())
        tmp.put_file(new_dir / "parquet" / f"cells_r{res}.parquet", f"releases/_new/parquet/cells_r{res}.parquet")
        old = pub.local_path(f"releases/{previous}/parquet/cells_r{res}.parquet")
        tmp.put_file(old, f"releases/{previous}/parquet/cells_r{res}.parquet")
        return diff(tmp, previous, "_new", res)
