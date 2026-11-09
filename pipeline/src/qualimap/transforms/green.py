"""Green space and canopy from the FSO Land Use Statistics (methodology §3.6).

NOLC04 sample points (100 m grid) are reclassified with ``catalog/maps/nolc04_classes.csv``,
counted per A5 cell, and summed over each cell's neighbourhood (``spherical_cap`` or
``grid_disk*``, catalog ``neighbourhood`` block). Shares are over non-excluded points; the
sealed share is floored at the current building-footprint share.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from .. import grid
from ..normalize import linear

CLASSES = ("sealed", "green_herb", "green_shrub", "tree", "excluded")


def load_class_map(path: str | Path) -> dict[int, str]:
    p = Path(path)
    if not p.is_absolute():
        root = Path(__file__).resolve()
        for parent in root.parents:
            if (parent / p).exists():
                p = parent / p
                break
    df = pd.read_csv(p, comment="#")
    if "code" in df.columns:
        return {int(c): cls for c, cls in zip(df["code"], df["class"], strict=False)}
    return {-int(c): cls for c, cls in zip(df["main_domain"], df["class"], strict=False)}  # by domain


def classify(codes: np.ndarray, cmap: dict[int, str]) -> np.ndarray:
    codes = np.asarray(codes, int)
    out = np.array([cmap.get(int(c), cmap.get(-(int(c) // 10), "excluded")) for c in codes])
    return out


def neighbourhood_shares(ctx, params: dict) -> pd.DataFrame:
    sid = params.get("source", "fso_arealstatistik_nolc04")
    nb = params.get("neighbourhood", {"method": "spherical_cap", "radius_m": params.get("radius_m", 300)})
    method = nb.get("method", "spherical_cap")
    radius = float(nb.get("radius_m", 300))
    k = nb.get("k")
    pts = pd.read_parquet(ctx.staged(sid))
    cmap = load_class_map(params.get("class_map", "catalog/maps/nolc04_classes.csv"))
    cls = classify(pts["code"].to_numpy(), cmap)
    lon, lat = grid.to_wgs84(pts["x"].to_numpy(float), pts["y"].to_numpy(float))
    pcells = grid.lonlat_to_cells(lon, lat, ctx.res)
    idx = ctx.index_of(pcells)
    n = len(ctx.cells)
    counts = {c: np.bincount(idx[(idx >= 0) & (cls == c)], minlength=n).astype(float) for c in CLASSES}
    u = ctx.universe
    kw = dict(method=method, radius_m=radius, k=k, res=ctx.res,
              lon=u["lon"].to_numpy(), lat=u["lat"].to_numpy())
    sums = {c: grid.neighbourhood_sum(ctx.cells, counts[c], **kw)[0] for c in CLASSES if c != "excluded"}
    land = sums["sealed"] + sums["green_herb"] + sums["green_shrub"] + sums["tree"]
    with np.errstate(invalid="ignore", divide="ignore"):
        green = (sums["green_herb"] + sums["green_shrub"] + sums["tree"]) / land
        canopy = sums["tree"] / land
        sealed = sums["sealed"] / land
    min_points = int(params.get("min_points", 10))
    green[land < min_points] = np.nan
    canopy[land < min_points] = np.nan
    sealed[land < min_points] = np.nan
    fp_sid = params.get("footprints_source", "swisstopo_swissbuildings3d")
    if ctx.has(fp_sid) and "footprints.parquet" in ctx.staged_names(fp_sid):
        fp = gpd.read_parquet(ctx.staged(fp_sid, "footprints.parquet"))
        fp_lv95 = fp.to_crs(grid.LV95)
        area = fp_lv95.area.to_numpy()
        c = fp_lv95.geometry.representative_point().to_crs("EPSG:4326")
        fcells = grid.lonlat_to_cells(c.x.to_numpy(), c.y.to_numpy(), ctx.res)
        fidx = ctx.index_of(fcells)
        fa = np.bincount(fidx[fidx >= 0], weights=area[fidx >= 0], minlength=n)
        fsum, fcnt = grid.neighbourhood_sum(ctx.cells, fa, **kw)
        with np.errstate(invalid="ignore", divide="ignore"):
            share = fsum / (fcnt * grid.cell_area(ctx.res))
        sealed = np.where(np.isnan(sealed), sealed, np.maximum(sealed, share))
    lin = params.get("linear", {})
    gs = linear(green, **lin.get("green_share", {"good": 0.6, "bad": 0.1}))
    cs = linear(canopy, **lin.get("canopy_share", {"good": 0.4, "bad": 0.05}))
    ss = linear(sealed, **lin.get("sealed_share", {"good": 0.1, "bad": 0.6}))
    b = params.get("blend", {"green_share": 0.4, "canopy_share": 0.3, "sealed_share": 0.3})
    score = b["green_share"] * gs + b["canopy_share"] * cs + b["sealed_share"] * ss
    score = np.where(np.isnan(green), np.nan, score)
    return pd.DataFrame({"cell_id": ctx.cells, "raw": score})
