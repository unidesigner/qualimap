"""Synthetic demo release for viewer development (``qm demo``).

Builds a small A5 universe for a bounding box, fills every catalog indicator with smooth
synthetic fields, adds synthetic communes, POIs and a noise raster, and runs the normal export
and publish code, so the release has exactly the production layout.
"""

from __future__ import annotations

import copy
import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from . import grid, normalize, releases
from .catalog import Catalog
from .export import build_release
from .state import StateStore, utcnow
from .store import Store

ZURICH = (8.47, 47.35, 8.59, 47.41)


def _field(lon, lat, cx, cy, scale, invert=False, seed=0):
    rng = np.random.default_rng(seed)
    d = np.hypot((lon - cx) * 75_000, (lat - cy) * 111_000)
    base = np.exp(-d / scale)
    wobble = 0.15 * np.sin(lon * 900 + seed) * np.cos(lat * 700 - seed) + rng.normal(0, 0.03, len(lon))
    v = np.clip(base + wobble, 0, 1)
    return 1 - v if invert else v


def build_demo(catalog: Catalog, pub: Store, bbox=ZURICH, res: int = 16, version: str | None = None,
               switch: bool = True, public_base_url: str | None = None) -> dict:
    cat = Catalog(copy.deepcopy(catalog.data), catalog.paths)
    cat.data["meta"]["grid"]["base_resolution"] = res
    cat.data["meta"]["grid"]["export_resolutions"] = [r for r in cat.export_resolutions if r <= res]
    tmp = Path(tempfile.mkdtemp(prefix="qm-demo-"))
    data = Store((tmp / "data").as_uri())
    state = StateStore(data)
    x0, y0, x1, y1 = bbox
    boundary = shapely.box(*bbox)
    # 3 × 3 synthetic communes
    xs, ys = np.linspace(x0, x1, 4), np.linspace(y0, y1, 4)
    polys, ids = [], []
    for i in range(3):
        for j in range(3):
            polys.append(shapely.box(xs[i], ys[j], xs[i + 1], ys[j + 1]))
            ids.append(9000 + i * 3 + j)
    communes = gpd.GeoDataFrame({"commune_bfs": ids, "name": [f"Demo {k - 8999}" for k in ids],
                                 "canton": 1}, geometry=polys, crs="EPSG:4326")
    uni = grid.build_universe(boundary, res, communes=communes)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    lon, lat = uni["lon"].to_numpy(), uni["lat"].to_numpy()
    uni["pop"] = (400 * _field(lon, lat, cx, cy, 2500, seed=1) * grid.cell_area(res) / 7917).astype(np.float32)
    _put_parquet(data, f"cells/universe_r{res}.parquet", uni)
    data.put_json(f"state/cells_r{res}.json", {"res": res, "cells": len(uni), "demo": True})
    _stage(data, state, cat.meta["region"]["boundary_source"], {"communes.parquet": communes,
                                                                "boundary.parquet": gpd.GeoDataFrame(geometry=[boundary], crs="EPSG:4326")})
    # POIs
    rng = np.random.default_rng(7)
    groups = list(cat.poi_groups)
    n = 400
    pois = pd.DataFrame({"osm_id": [f"n{i}" for i in range(n)],
                         "lon": rng.uniform(x0, x1, n), "lat": rng.normal(cy, (y1 - y0) / 5, n).clip(y0, y1),
                         "name": [f"Demo POI {i}" for i in range(n)],
                         "groups": [[groups[i % len(groups)]] for i in range(n)]})
    _stage(data, state, "osm_switzerland", {"pois.parquet": pois})
    # synthetic noise raster (LV95)
    _stage_raster(data, state, "foen_sonbase_road_day", bbox, lambda X, Y: 40 + 30 * np.exp(-np.abs(Y - Y.mean()) / 300))
    fields = {
        "noise": lambda: 40 + 30 * _field(lon, lat, cx, cy, 1500, seed=2),
        "air": lambda: 0.4 + 0.9 * _field(lon, lat, cx, cy, 2000, seed=3),
        "public_transport": lambda: np.round(4 * _field(lon, lat, cx, cy, 2200, seed=4)) * 2.5,
        "education": lambda: 10 * _field(lon, lat, cx - 0.01, cy, 3000, seed=5),
        "dining": lambda: 10 * _field(lon, lat, cx, cy, 1200, seed=6),
        "health": lambda: 10 * _field(lon, lat, cx + 0.01, cy, 2500, seed=7),
        "culture": lambda: 10 * _field(lon, lat, cx, cy + 0.005, 1500, seed=8),
        "green": lambda: 10 * _field(lon, lat, cx, cy, 2000, invert=True, seed=9),
        "rooftop_solar": lambda: np.round(1 + 4 * _field(lon, lat, cx, cy, 4000, invert=True, seed=10)),
    }
    for ind in cat.indicators.values():
        gen = fields.get(ind.id, lambda: 10 * _field(lon, lat, cx, cy, 2000, seed=11))
        raw = gen()
        if ind.level == "commune":
            per = pd.Series(raw).groupby(uni["commune_bfs"].to_numpy()).mean()
            raw = 20000 + 8000 * (per / per.max())
            score = normalize.apply(raw.to_numpy(), ind.normalize, None)
            m = dict(zip(per.index, score, strict=False))
            r = dict(zip(per.index, raw, strict=False))
            score = uni["commune_bfs"].map(m).to_numpy(float)
            raw = uni["commune_bfs"].map(r).to_numpy(float)
        else:
            score = normalize.apply(raw, ind.normalize, uni["pop"].to_numpy())
        df = pd.DataFrame({"cell_id": uni["cell_id"].to_numpy(np.uint64), "raw": raw.astype(np.float32),
                           "score": score.astype(np.float32),
                           "q": normalize.quantize(score), "flags": np.zeros(len(uni), np.uint32)})
        key = f"indicators/{ind.id}/demo/r{res}.parquet"
        _put_parquet(data, key, df)
        state.save_indicator_record(ind.id, {"indicator_id": ind.id, "hash": "demo", "res": res, "key": key,
                                             "computed_at": utcnow().isoformat(), "qa_passed": True,
                                             "stats": {"cells": len(df)}})
    version = version or releases.next_version(pub)
    out = tmp / "release"
    manifest = build_release(cat, data, state, out, version)
    pub_info = releases.publish(pub, out, version, switch=switch, public_base_url=public_base_url)
    return {"version": version, "cells": len(uni), "files": pub_info["files"], "pub": pub.url,
            "resolutions": manifest["resolutions"]}


def _put_parquet(store: Store, key: str, df) -> None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.parquet"
        df.to_parquet(p, index=False)
        store.put_file(p, key)


def _stage(store: Store, state: StateStore, sid: str, files: dict) -> None:
    keys = {}
    for name, df in files.items():
        key = f"staged/{sid}/demo/{name}"
        _put_parquet(store, key, df)
        keys[name] = key
    prev = state.staged_record(sid) or {"files": {}}
    state.save_staged_record(sid, {"source_id": sid, "raw_sha256": "demo", "files": prev["files"] | keys})


def _stage_raster(store: Store, state: StateStore, sid: str, bbox, fn) -> None:
    import rasterio
    from rasterio.transform import from_origin

    xa, ya = grid.to_lv95([bbox[0], bbox[2]], [bbox[1], bbox[3]])
    px = 20.0
    w, h = int((xa[1] - xa[0]) / px), int((ya[1] - ya[0]) / px)
    X, Y = np.meshgrid(xa[0] + np.arange(w) * px, ya[1] - np.arange(h) * px)
    arr = fn(X, Y).astype(np.float32)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "data.tif"
        with rasterio.open(p, "w", driver="GTiff", width=w, height=h, count=1, dtype="float32",
                           crs="EPSG:2056", transform=from_origin(xa[0], ya[1], px, px), nodata=-9999) as ds:
            ds.write(arr, 1)
        key = f"staged/{sid}/demo/data.tif"
        store.put_file(p, key)
    state.save_staged_record(sid, {"source_id": sid, "raw_sha256": "demo", "files": {"data.tif": key}})
