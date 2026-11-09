"""Cell universe build and dasymetric population (SPEC §3.4; methodology §1.2)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import shapely

from . import grid
from .catalog import Catalog
from .staging import load_boundary, load_communes, staged_path
from .state import StateStore
from .store import Store, sha256_bytes

RESIDENTIAL_GKAT = (1020, 1030, 1040)
EXISTING_GSTAT = 1004


def build_cells(catalog: Catalog, store: Store, state: StateStore, res: int | None = None,
                bbox: tuple[float, float, float, float] | None = None) -> dict:
    """Build ``cells/universe_r{res}.parquet`` and ``cells/buildings_r{res}.parquet``."""
    res = res or catalog.base_resolution
    boundary = load_boundary(catalog, store, state)
    if bbox:
        boundary = boundary.intersection(shapely.box(*bbox))
    communes = load_communes(catalog, store, state)
    water = _water(catalog, store, state)
    uni = grid.build_universe(boundary, res, communes=communes, water=water)
    buildings = build_buildings(catalog, store, state, res, uni)
    if len(buildings):
        pop = buildings.groupby("cell_id")["pop"].sum()
        uni["pop"] = uni["cell_id"].map(pop).fillna(0.0).astype(np.float32)
    else:
        uni["pop"] = np.float32(0)
    _save(store, f"cells/universe_r{res}.parquet", uni)
    _save(store, f"cells/buildings_r{res}.parquet", buildings)
    info = {"res": res, "cells": len(uni), "buildings": len(buildings),
            "population": float(uni["pop"].sum()), "bbox": list(bbox) if bbox else None,
            "ids_sha256": sha256_bytes(uni["cell_id"].to_numpy(np.uint64).tobytes()),
            "inputs": {sid: (state.staged_record(sid) or {}).get("raw_sha256")
                       for sid in (catalog.meta.get("region", {}).get("boundary_source"), "fso_gwr", "fso_statpop")}}
    store.put_json(f"state/cells_r{res}.json", info)
    return info


def _save(store: Store, key: str, df: pd.DataFrame) -> None:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.parquet"
        df.to_parquet(p, index=False)
        store.put_file(p, key)


def _water(catalog: Catalog, store: Store, state: StateStore):
    import geopandas as gpd

    sid = catalog.meta.get("region", {}).get("water_source")
    if not sid or not state.staged_record(sid):
        return None
    try:
        return gpd.read_parquet(staged_path(store, state, sid, "water.parquet"))
    except Exception:
        return None


def build_buildings(catalog: Catalog, store: Store, state: StateStore, res: int,
                    universe: pd.DataFrame) -> pd.DataFrame:
    """Residential buildings with population pushed down from the STATPOP hectare grid."""
    if not state.staged_record("fso_gwr"):
        return pd.DataFrame(columns=["egid", "lon", "lat", "cell_id", "commune_bfs", "dwellings", "pop"])
    gwr = pd.read_parquet(staged_path(store, state, "fso_gwr"))
    if "status" in gwr.columns:
        gwr = gwr[pd.to_numeric(gwr["status"], errors="coerce") == EXISTING_GSTAT]
    if "category" in gwr.columns:
        gwr = gwr[pd.to_numeric(gwr["category"], errors="coerce").isin(RESIDENTIAL_GKAT)]
    gwr = gwr.dropna(subset=["x", "y"]).copy()
    gwr["dwellings"] = pd.to_numeric(gwr.get("dwellings", 1), errors="coerce").fillna(1).clip(lower=1)
    lon, lat = grid.to_wgs84(gwr["x"].to_numpy(float), gwr["y"].to_numpy(float))
    gwr["lon"], gwr["lat"] = lon, lat
    gwr["cell_id"] = grid.lonlat_to_cells(lon, lat, res)
    keep = np.isin(gwr["cell_id"].to_numpy(np.uint64), universe["cell_id"].to_numpy(np.uint64))
    gwr = gwr[keep].copy()
    gwr["pop"] = 0.0
    if state.staged_record("fso_statpop") and len(gwr):
        sp = pd.read_parquet(staged_path(store, state, "fso_statpop"))
        sp["hx"] = (pd.to_numeric(sp["x"]) // 100).astype(np.int64)
        sp["hy"] = (pd.to_numeric(sp["y"]) // 100).astype(np.int64)
        hpop = sp.groupby(["hx", "hy"])["pop"].sum()
        gwr["hx"] = (gwr["x"].astype(float) // 100).astype(np.int64)
        gwr["hy"] = (gwr["y"].astype(float) // 100).astype(np.int64)
        dsum = gwr.groupby(["hx", "hy"])["dwellings"].transform("sum")
        hp = pd.Series(list(zip(gwr["hx"], gwr["hy"], strict=False))).map(hpop).fillna(0).to_numpy()
        gwr["pop"] = hp * gwr["dwellings"].to_numpy() / dsum.to_numpy()
        # hectares with residents but no residential building: centroid cell gets the total
        have = set(zip(gwr["hx"], gwr["hy"], strict=False))
        orphan = hpop[[k not in have for k in hpop.index]]
        if len(orphan):
            ox = np.array([k[0] for k in orphan.index]) * 100 + 50
            oy = np.array([k[1] for k in orphan.index]) * 100 + 50
            olon, olat = grid.to_wgs84(ox, oy)
            ocell = grid.lonlat_to_cells(olon, olat, res)
            extra = pd.DataFrame({"egid": -1, "x": ox, "y": oy, "lon": olon, "lat": olat,
                                  "cell_id": ocell, "dwellings": 0, "pop": orphan.to_numpy(float),
                                  "commune_bfs": 0})
            inside = np.isin(ocell, universe["cell_id"].to_numpy(np.uint64))
            gwr = pd.concat([gwr, extra[inside]], ignore_index=True)
    cols = [c for c in ["egid", "lon", "lat", "x", "y", "cell_id", "commune_bfs", "dwellings", "pop"] if c in gwr]
    out = gwr[cols].copy()
    out["cell_id"] = out["cell_id"].astype(np.uint64)
    if "commune_bfs" in out:
        out["commune_bfs"] = pd.to_numeric(out["commune_bfs"], errors="coerce").fillna(0).astype(np.int32)
    return out.reset_index(drop=True)
