"""Build a complete release directory (GeoParquet, GeoZarr, communes, POIs, rasters, manifest)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from .. import grid
from ..catalog import Catalog
from ..staging import staged_path
from ..state import StateStore, utcnow
from ..store import Store, sha256_file
from ..transforms import FLAGS
from . import geoparquet, geozarr, tables

ZOOM_TABLE = [
    {"min_zoom": 0, "max_zoom": 8, "geography": "communes"},
    {"min_zoom": 8, "max_zoom": 10, "resolution": 11},
    {"min_zoom": 10, "max_zoom": 11, "resolution": 12},
    {"min_zoom": 11, "max_zoom": 12, "resolution": 13},
    {"min_zoom": 12, "max_zoom": 13, "resolution": 14},
    {"min_zoom": 13, "max_zoom": 14, "resolution": 15},
    {"min_zoom": 14, "max_zoom": 24, "resolution": 16},
]


def git_sha() -> str | None:
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True, cwd=Path(__file__).parent).stdout.strip()
    except Exception:
        return None


def a5_version() -> str:
    from importlib.metadata import version

    try:
        return version("pya5")
    except Exception:
        return "unknown"


def build_release(catalog: Catalog, store: Store, state: StateStore, out_dir: Path, version: str,
                  formats: tuple[str, ...] = ("parquet", "zarr", "raster-zarr"),
                  resolutions: list[int] | None = None) -> dict:
    base_res = catalog.base_resolution
    resolutions = sorted(set(resolutions or catalog.export_resolutions))
    resolutions = [r for r in resolutions if r <= base_res]
    base, present = tables.base_table(catalog, store, state, base_res)
    if not present:
        raise RuntimeError("no computed indicators; run `qm compute --all` first")
    release_meta = {"release": version, "a5_version": a5_version(), "base_resolution": base_res}
    finals: dict[int, pd.DataFrame] = {}
    for r in resolutions:
        finals[r] = tables.finalize(tables.rollup(catalog, base, present, base_res, r), present, catalog)
    out_dir.mkdir(parents=True, exist_ok=True)
    files_info: dict = {}
    if "parquet" in formats:
        for r, df in finals.items():
            files_info[f"parquet/cells_r{r}.parquet"] = geoparquet.write_cells(
                df, out_dir / "parquet" / f"cells_r{r}.parquet", r, release_meta)
        communes = _communes(catalog, store, state, base, present)
        if communes is not None:
            geoparquet.write_polygons(communes, out_dir / "parquet" / "communes.parquet")
            geoparquet.write_geoarrow_polygons(communes, out_dir / "arrow" / "communes.arrow")
            files_info["parquet/communes.parquet"] = {"rows": len(communes)}
            files_info["arrow/communes.arrow"] = {"rows": len(communes)}
        pois = _pois(catalog, store, state, base_res)
        if pois is not None:
            geoparquet.write_points(pois, out_dir / "parquet" / "pois.parquet")
            geoparquet.write_geoarrow_points(pois, out_dir / "arrow" / "pois.arrow")
            files_info["parquet/pois.parquet"] = {"rows": len(pois)}
            files_info["arrow/pois.arrow"] = {"rows": len(pois)}
    if "zarr" in formats:
        files_info["zarr/qualimap.zarr"] = geozarr.write_dggs(finals, out_dir / "zarr" / "qualimap.zarr",
                                                              present, release_meta)
    rasters = {}
    if "raster-zarr" in formats:
        bounds = _boundary_bounds_lv95(base)
        for s in catalog.sources.values():
            if s.export_raster_zarr and state.staged_record(s.id):
                info = geozarr.write_raster(staged_path(store, state, s.id), out_dir / "zarr" / "rasters" / f"{s.id}.zarr",
                                            bounds)
                rasters[s.id] = info
    catalog_json = catalog.resolved_view() | {
        "release": version,
        "present_indicators": present,
        "flags": FLAGS,
        "zoom_table": [z for z in ZOOM_TABLE if z.get("resolution", 0) in resolutions or "geography" in z],
        "resolutions": resolutions,
        "tile_resolutions": {str(r): max(r - 5, 0) for r in resolutions},
        "files": {
            "cells": {str(r): f"parquet/cells_r{r}.parquet" for r in resolutions} if "parquet" in formats else {},
            "communes": "parquet/communes.parquet" if "parquet/communes.parquet" in files_info else None,
            "pois": "parquet/pois.parquet" if "parquet/pois.parquet" in files_info else None,
            "communes_arrow": "arrow/communes.arrow" if "arrow/communes.arrow" in files_info else None,
            "pois_arrow": "arrow/pois.arrow" if "arrow/pois.arrow" in files_info else None,
            "zarr": "zarr/qualimap.zarr" if "zarr" in formats else None,
            "rasters": {sid: {"path": f"zarr/rasters/{sid}.zarr", **meta} for sid, meta in rasters.items()},
        },
        "bounds": [float(base["lon"].min()), float(base["lat"].min()), float(base["lon"].max()), float(base["lat"].max())],
    }
    _write_json(out_dir / "catalog.json", catalog_json)
    manifest = {
        "version": version,
        "created_at": utcnow().isoformat(),
        "catalog_version": catalog.meta.get("catalog_version"),
        "git_sha": git_sha(),
        "a5_version": a5_version(),
        "base_resolution": base_res,
        "resolutions": resolutions,
        "sources": _source_states(catalog, state),
        "indicators": {i: _ind_summary(state, i) for i in present},
        "files": [],
        "outputs": files_info,
    }
    for f in sorted(out_dir.rglob("*")):
        if f.is_file() and f.name != "manifest.json":
            manifest["files"].append({"path": f.relative_to(out_dir).as_posix(), "bytes": f.stat().st_size,
                                      "sha256": sha256_file(f)})
    _write_json(out_dir / "manifest.json", manifest)
    return manifest


def _write_json(path: Path, obj) -> None:
    import json

    path.write_text(json.dumps(obj, indent=2, default=str))


def _source_states(catalog: Catalog, state: StateStore) -> dict:
    out = {}
    for sid in catalog.sources:
        rec = state.fetch_record(sid)
        out[sid] = None if rec is None else {"sha256": rec.sha256, "fetched_at": rec.fetched_at,
                                              "status": rec.status}
    return out


def _ind_summary(state: StateStore, iid: str) -> dict:
    rec = state.indicator_record(iid) or {}
    return {k: rec.get(k) for k in ("hash", "computed_at", "stats", "qa_passed")}


def _boundary_bounds_lv95(base: pd.DataFrame) -> tuple:
    x, y = grid.to_lv95(base["lon"].to_numpy(), base["lat"].to_numpy())
    pad = 2000
    return (float(x.min()) - pad, float(y.min()) - pad, float(x.max()) + pad, float(y.max()) + pad)


def _communes(catalog: Catalog, store: Store, state: StateStore, base: pd.DataFrame, present: list[str]):
    region = catalog.meta.get("region", {})
    sid = region.get("boundary_source")
    t = tables.commune_table(catalog, base, present)
    for i in present:
        from .. import normalize

        t[f"q_{i}"] = normalize.quantize(t[f"score_{i}"].to_numpy(float), catalog.quant_scale, catalog.nodata)
    cols = ["commune_bfs", "pop"] + [f"q_{i}" for i in present] + [f"raw_{i}" for i in present]
    t = t[cols]
    try:
        polys = gpd.read_parquet(staged_path(store, state, sid, region.get("commune_file", "communes.parquet")))
    except Exception:
        return None
    polys = polys.dissolve(by="commune_bfs", as_index=False, aggfunc="first")
    polys["commune_bfs"] = polys["commune_bfs"].astype(np.uint16)
    keep = [c for c in ("commune_bfs", "name", "canton", "geometry") if c in polys]
    polys = polys[keep].to_crs("EPSG:4326")
    polys["geometry"] = polys.geometry.simplify(float(region.get("commune_simplify_deg", 0.0003)), preserve_topology=True)
    out = polys.merge(t, on="commune_bfs", how="inner")
    return gpd.GeoDataFrame(out, geometry="geometry", crs="EPSG:4326")


def _pois(catalog: Catalog, store: Store, state: StateStore, res: int):
    sid = "osm_switzerland"
    rec = state.staged_record(sid)
    if not rec or "pois.parquet" not in rec.get("files", {}):
        return None
    pois = pd.read_parquet(staged_path(store, state, sid, "pois.parquet"))
    if pois.empty:
        return None
    group_dim = {}
    for ind in catalog.indicators.values():
        for st in ind.params.get("subtypes", []) or []:
            group_dim[st["group"]] = ind.dimension
    pois = pois.copy()
    pois["group"] = pois["groups"].apply(lambda gs: list(gs)[0] if len(gs) else None)
    pois["dimension"] = pois["group"].map(group_dim)
    pois["cell_id_r16"] = grid.lonlat_to_cells(pois["lon"].to_numpy(), pois["lat"].to_numpy(), res)
    pois = pois.drop(columns=["groups"]).sort_values("cell_id_r16").reset_index(drop=True)
    pois["name"] = pois["name"].fillna("")
    return pois[["osm_id", "group", "dimension", "name", "cell_id_r16", "lon", "lat"]]
