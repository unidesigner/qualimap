"""A5 grid operations: coverings, universe, joins, neighbourhoods and roll-up (SPEC §3).

All cell IDs are carried as ``numpy.uint64``. The pya5 Rust backend is used for cell maths;
DuckDB's ``a5`` extension (see :mod:`qualimap.duck`) is an optional accelerator for SQL joins.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from functools import lru_cache

import a5
import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from pyproj import Transformer
from scipy.spatial import cKDTree
from shapely.geometry import MultiPolygon, Polygon, mapping

WGS84 = "EPSG:4326"
LV95 = "EPSG:2056"
ROLLUP_FLOOR = 11  # res-11 is the partition / tile key (SPEC §3.2)

_to_lv95 = Transformer.from_crs(WGS84, LV95, always_xy=True)
_to_wgs = Transformer.from_crs(LV95, WGS84, always_xy=True)


def to_lv95(lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    return _to_lv95.transform(np.asarray(lon, float), np.asarray(lat, float))


def to_wgs84(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    return _to_wgs.transform(np.asarray(x, float), np.asarray(y, float))


def u64(values: Iterable[int]) -> np.ndarray:
    return np.fromiter((int(v) for v in values), dtype=np.uint64)


def cell_area(res: int) -> float:
    return float(a5.cell_area(res))


# ------------------------------------------------------------------------ point / cell
def lonlat_to_cells(lon: Sequence[float], lat: Sequence[float], res: int) -> np.ndarray:
    lon = np.asarray(lon, float)
    lat = np.asarray(lat, float)
    out = np.empty(len(lon), dtype=np.uint64)
    f = a5.lonlat_to_cell
    for i in range(len(lon)):
        out[i] = f((float(lon[i]), float(lat[i])), res)
    return out


def cell_centroids(cells: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lon = np.empty(len(cells))
    lat = np.empty(len(cells))
    f = a5.cell_to_lonlat
    for i, c in enumerate(cells):
        lon[i], lat[i] = f(int(c))
    return lon, lat


def parents(cells: np.ndarray, res: int) -> np.ndarray:
    out = np.empty(len(cells), dtype=np.uint64)
    f = a5.cell_to_parent
    for i, c in enumerate(cells):
        out[i] = f(int(c), res)
    return out


def boundary_ring(cell: int) -> list[tuple[float, float]]:
    return [tuple(p) for p in a5.cell_to_boundary(int(cell), {"closed_ring": True, "segments": 1})]


def cell_polygons(cells: np.ndarray) -> list[Polygon]:
    return [Polygon(boundary_ring(int(c))) for c in cells]


def descendant_range(cell: int, res: int) -> tuple[int, int]:
    """First and last descendant ID at ``res`` (contiguous on the A5 curve)."""
    if a5.get_resolution(int(cell)) == res:
        return int(cell), int(cell)
    kids = a5.cell_to_children(int(cell), res)
    return int(kids[0]), int(kids[-1])


# ------------------------------------------------------------------------ coverings
def _polygon_parts(geom) -> list[Polygon]:
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    if hasattr(geom, "geoms"):
        return [g for part in geom.geoms for g in _polygon_parts(part)]
    return []


def cells_for_polygon(geom, res: int, overlapping: bool = False) -> np.ndarray:
    """Uncompacted cells covering ``geom`` (WGS84). Centre containment unless ``overlapping``."""
    opts = {"containment": "overlapping"} if overlapping else None
    out: list[int] = []
    for poly in _polygon_parts(geom):
        if poly.is_empty:
            continue
        rings = [list(map(tuple, poly.exterior.coords))] + [
            list(map(tuple, r.coords)) for r in poly.interiors
        ]
        covering = a5.polygon_to_cells(rings, res, opts)
        if covering:
            out.extend(a5.uncompact(covering))
    if not out:
        return np.empty(0, dtype=np.uint64)
    return np.unique(u64(out))


def densify_lv95(geom_wgs84, max_segment_m: float = 50.0):
    """Densify in LV95 so edges stay close to the source boundary after reprojection."""
    g = shapely.transform(geom_wgs84, lambda xy: np.column_stack(to_lv95(xy[:, 0], xy[:, 1])))
    g = shapely.segmentize(g, max_segment_m)
    return shapely.transform(g, lambda xy: np.column_stack(to_wgs84(xy[:, 0], xy[:, 1])))


# ------------------------------------------------------------------------ universe
def build_universe(
    boundary_wgs84,
    res: int,
    communes: gpd.GeoDataFrame | None = None,
    water: gpd.GeoDataFrame | None = None,
    commune_id: str = "commune_bfs",
) -> pd.DataFrame:
    """Cell universe for a region (SPEC §3.4)."""
    boundary = densify_lv95(boundary_wgs84)
    inner = cells_for_polygon(boundary, res)
    outer = cells_for_polygon(boundary, res, overlapping=True)
    cells = np.union1d(inner, outer)
    border = np.setdiff1d(cells, inner)
    lon, lat = cell_centroids(cells)
    df = pd.DataFrame({"cell_id": cells, "lon": lon, "lat": lat})
    df["in_share"] = np.float32(1.0)
    if len(border):
        border_polys = gpd.GeoSeries(cell_polygons(border), crs=WGS84).to_crs(LV95)
        b_lv95 = gpd.GeoSeries([boundary], crs=WGS84).to_crs(LV95).iloc[0]
        inter = border_polys.intersection(b_lv95).area.to_numpy()
        share = np.clip(inter / border_polys.area.to_numpy(), 0, 1).astype(np.float32)
        idx = np.searchsorted(cells, border)
        df.loc[idx, "in_share"] = share
        df = df[df["in_share"] > 0].reset_index(drop=True)
    for r in range(max(ROLLUP_FLOOR, 0), res):
        df[f"parent_r{r}"] = parents(df["cell_id"].to_numpy(np.uint64), r)
    df["commune_bfs"] = assign_polygons(df, communes, commune_id) if communes is not None else 0
    df["commune_bfs"] = df["commune_bfs"].fillna(0).astype(np.uint16)
    if water is not None and len(water):
        pts = gpd.GeoDataFrame(geometry=gpd.points_from_xy(df.lon, df.lat), crs=WGS84)
        hit = gpd.sjoin(pts, water[["geometry"]].to_crs(WGS84), predicate="within", how="left")
        df["is_water"] = ~hit.groupby(level=0)["index_right"].first().isna().to_numpy()
    else:
        df["is_water"] = False
    return df.sort_values("cell_id", kind="stable").reset_index(drop=True)


def assign_polygons(cells_df: pd.DataFrame, polys: gpd.GeoDataFrame, id_col: str) -> pd.Series:
    """Centroid-in-polygon assignment, nearest polygon for border cells outside every polygon."""
    pts = gpd.GeoDataFrame(
        {"_i": np.arange(len(cells_df))},
        geometry=gpd.points_from_xy(cells_df.lon, cells_df.lat),
        crs=WGS84,
    ).to_crs(LV95)
    pl = polys[[id_col, "geometry"]].to_crs(LV95)
    hit = gpd.sjoin(pts, pl, predicate="within", how="left").drop_duplicates("_i").set_index("_i")
    out = hit[id_col].reindex(range(len(cells_df)))
    missing = out.isna().to_numpy()
    if missing.any():
        near = gpd.sjoin_nearest(pts[missing], pl, how="left").drop_duplicates("_i").set_index("_i")
        out.loc[near.index] = near[id_col]
    return out.reset_index(drop=True)


def area_shares(cells: np.ndarray, polys: gpd.GeoDataFrame, id_col: str) -> pd.DataFrame:
    """Exact area shares of each cell in each polygon (used for boundary cells only)."""
    cp = gpd.GeoDataFrame({"cell_id": cells}, geometry=cell_polygons(cells), crs=WGS84).to_crs(LV95)
    pl = polys[[id_col, "geometry"]].to_crs(LV95)
    inter = gpd.overlay(cp, pl, how="intersection", keep_geom_type=True)
    inter["share"] = inter.area / cell_area(a5.get_resolution(int(cells[0])))
    return inter[["cell_id", id_col, "share"]]


# ------------------------------------------------------------------------ neighbourhoods
@lru_cache(maxsize=64)
def disk_stats(res: int, k: int, vertex: bool = False, lon: float = 8.2, lat: float = 46.8) -> dict:
    """Cell count and equivalent radius of a k-step disk at a reference location."""
    c = a5.lonlat_to_cell((lon, lat), res)
    fn = a5.grid_disk_vertex if vertex else a5.grid_disk
    n = len(_disk_cells(fn(c, k), res))
    return {"cells": n, "equivalent_radius_m": math.sqrt(n * cell_area(res) / math.pi)}


def _disk_cells(result, res: int) -> list[int]:
    cells = list(result)
    if cells and a5.is_compaction_marker(cells[-1]):
        cells = list(a5.uncompact(cells))
    return [c for c in cells if a5.get_resolution(c) == res]


def derive_k(res: int, radius_m: float, vertex: bool = False, k_max: int = 64) -> int:
    """k whose disk equivalent radius √(n·A/π) is closest to ``radius_m`` (SPEC §3.5)."""
    best_k, best = 1, float("inf")
    for k in range(1, k_max + 1):
        er = disk_stats(res, k, vertex)["equivalent_radius_m"]
        if abs(er - radius_m) < best:
            best_k, best = k, abs(er - radius_m)
        if er > radius_m * 1.5:
            break
    return best_k


def neighbourhood_sum(
    cells: np.ndarray,
    values: np.ndarray,
    method: str = "spherical_cap",
    radius_m: float | None = None,
    k: int | None = None,
    res: int | None = None,
    lon: np.ndarray | None = None,
    lat: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Sum of ``values`` and number of member cells over each cell's neighbourhood.

    ``spherical_cap`` selects cells whose centre lies within ``radius_m`` (the semantics of
    ``a5_spherical_cap``), computed with a KD-tree on LV95 centroids. ``grid_disk`` and
    ``grid_disk_vertex`` use the A5 step neighbourhoods; ``k`` is derived from ``radius_m``
    when not given. Neighbours outside ``cells`` contribute nothing.
    """
    cells = np.asarray(cells, dtype=np.uint64)
    values = np.asarray(values, dtype=float)
    if method == "spherical_cap":
        if radius_m is None:
            raise ValueError("spherical_cap needs radius_m")
        if lon is None or lat is None:
            lon, lat = cell_centroids(cells)
        x, y = to_lv95(lon, lat)
        tree = cKDTree(np.column_stack([x, y]))
        members = tree.query_ball_point(np.column_stack([x, y]), r=radius_m)
        sums = np.fromiter((values[m].sum() for m in members), float, len(cells))
        counts = np.fromiter((len(m) for m in members), float, len(cells))
        return sums, counts
    if method not in ("grid_disk", "grid_disk_vertex"):
        raise ValueError(f"unknown neighbourhood method {method!r}")
    res = res if res is not None else a5.get_resolution(int(cells[0]))
    vertex = method == "grid_disk_vertex"
    k = k or derive_k(res, float(radius_m or 0), vertex)
    fn = a5.grid_disk_vertex if vertex else a5.grid_disk
    order = np.argsort(cells)
    sorted_cells = cells[order]
    sums = np.zeros(len(cells))
    counts = np.zeros(len(cells))
    for i, c in enumerate(cells):
        nb = u64(_disk_cells(fn(int(c), k), res))
        idx = order[_match(sorted_cells, nb)]
        sums[i] = values[idx].sum()
        counts[i] = len(idx)
    return sums, counts


def _match(sorted_cells: np.ndarray, nb: np.ndarray) -> np.ndarray:
    pos = np.searchsorted(sorted_cells, nb)
    ok = pos < len(sorted_cells)
    pos, nb = pos[ok], nb[ok]
    return pos[sorted_cells[pos] == nb]


# ------------------------------------------------------------------------ roll-up
def weighted_rollup(
    df: pd.DataFrame,
    parent_col: str,
    value_cols: list[str],
    weight: np.ndarray | None,
    energetic: Sequence[str] = (),
    maximum: Sequence[str] = (),
) -> pd.DataFrame:
    """Aggregate value columns to parents. NaN values are excluded from numerator and weights.

    Columns in ``energetic`` are averaged in the energy domain (10·log10 mean of 10^(L/10)).
    """
    w = np.ones(len(df)) if weight is None else np.asarray(weight, float)
    keys, codes = np.unique(df[parent_col].to_numpy(np.uint64), return_inverse=True)
    result = pd.DataFrame({"cell_id": keys})
    for col in value_cols:
        v = df[col].to_numpy(float)
        if col in maximum:
            agg = pd.Series(v).groupby(codes).max().reindex(range(len(keys))).to_numpy()
            result[col] = agg
            continue
        vv = 10 ** (v / 10.0) if col in energetic else v
        ok = ~np.isnan(vv)
        num = np.bincount(codes[ok], weights=(vv * w)[ok], minlength=len(keys))
        den = np.bincount(codes[ok], weights=w[ok], minlength=len(keys))
        # fall back to an unweighted mean where all weights are zero (e.g. unpopulated parents)
        cnt = np.bincount(codes[ok], weights=vv[ok], minlength=len(keys))
        n = np.bincount(codes[ok], minlength=len(keys)).astype(float)
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = np.where(den > 0, num / den, np.where(n > 0, cnt / n, np.nan))
            result[col] = 10 * np.log10(mean) if col in energetic else mean
    return result


def geojson_polygon(cell: int) -> dict:
    return mapping(Polygon(boundary_ring(cell)))
