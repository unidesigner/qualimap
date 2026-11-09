"""GeoParquet 1.1 writers (SPEC §6.1, §6.3, §6.5)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import shapely

from .. import grid

GEO_VERSION = "1.1.0"


def _geo_metadata(geometry_types: list[str], extra: dict, covering: bool = True) -> dict:
    col = {"encoding": "WKB", "geometry_types": geometry_types}
    if covering:
        col["covering"] = {"bbox": {"xmin": ["bbox", "xmin"], "ymin": ["bbox", "ymin"],
                                    "xmax": ["bbox", "xmax"], "ymax": ["bbox", "ymax"]}}
    return {"version": GEO_VERSION, "primary_column": "geometry", "columns": {"geometry": col}} | extra


def _bbox_struct(xmin, ymin, xmax, ymax) -> pa.StructArray:
    return pa.StructArray.from_arrays(
        [pa.array(np.asarray(a, np.float32)) for a in (xmin, ymin, xmax, ymax)],
        names=["xmin", "ymin", "xmax", "ymax"],
    )


def cell_bboxes(cells: np.ndarray) -> tuple[np.ndarray, ...]:
    xmin = np.empty(len(cells)); ymin = np.empty(len(cells))  # noqa: E702
    xmax = np.empty(len(cells)); ymax = np.empty(len(cells))  # noqa: E702
    for i, c in enumerate(cells):
        ring = np.asarray(grid.boundary_ring(int(c)))
        xmin[i], ymin[i] = ring.min(axis=0)
        xmax[i], ymax[i] = ring.max(axis=0)
    return xmin, ymin, xmax, ymax


def cells_to_arrow(df: pd.DataFrame) -> pa.Table:
    cells = df["cell_id"].to_numpy(np.uint64)
    pts = shapely.points(df["lon"].to_numpy(), df["lat"].to_numpy())
    cols = {
        "cell_id": pa.array(cells, pa.uint64()),
        "geometry": pa.array(shapely.to_wkb(pts), pa.binary()),
        "bbox": _bbox_struct(*cell_bboxes(cells)),
    }
    for c in df.columns:
        if c in ("cell_id", "lon", "lat", "parent_r11"):
            continue
        cols[c] = pa.array(df[c].to_numpy())
    return pa.table(cols)


def write_cells(df: pd.DataFrame, path: Path, res: int, release_meta: dict) -> dict:
    """One row group per res-11 parent (coarser files: fixed 4,096-row groups)."""
    df = df.sort_values("cell_id", kind="stable").reset_index(drop=True)
    table = cells_to_arrow(df)
    meta = _geo_metadata(["Point"], {})
    schema = table.schema.with_metadata({
        b"geo": json.dumps(meta).encode(),
        b"qualimap": json.dumps(release_meta | {"a5_resolution": res}).encode(),
    })
    table = table.replace_schema_metadata(schema.metadata)
    path.parent.mkdir(parents=True, exist_ok=True)
    groups = 0
    with pq.ParquetWriter(path, schema, compression="zstd", compression_level=9,
                          write_page_index=True, write_statistics=True) as w:
        if res > 11 and "parent_r11" in df:
            parents = df["parent_r11"].to_numpy(np.uint64)
            bounds = np.flatnonzero(np.diff(parents)) + 1
            starts = np.concatenate([[0], bounds])
            ends = np.concatenate([bounds, [len(df)]])
            # rows are sorted by cell_id, and res-11 descendants are contiguous, so each run of
            # equal parents is exactly one parent's children
            for s, e in zip(starts, ends, strict=False):
                w.write_table(table.slice(int(s), int(e - s)))
                groups += 1
        else:
            for s in range(0, len(df), 4096):
                w.write_table(table.slice(s, min(4096, len(df) - s)))
                groups += 1
    return {"rows": len(df), "row_groups": groups}


def write_polygons(gdf, path: Path, extra_meta: dict | None = None) -> None:
    geoms = gdf.geometry.to_numpy()
    xmin, ymin, xmax, ymax = shapely.bounds(geoms).T
    cols = {c: pa.array(gdf[c].to_numpy()) for c in gdf.columns if c != "geometry"}
    cols["geometry"] = pa.array(shapely.to_wkb(geoms), pa.binary())
    cols["bbox"] = _bbox_struct(xmin, ymin, xmax, ymax)
    table = pa.table(cols)
    types = sorted({g.geom_type for g in geoms if g is not None})
    meta = _geo_metadata(types, extra_meta or {})
    table = table.replace_schema_metadata({b"geo": json.dumps(meta).encode()})
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")


def write_points(df: pd.DataFrame, path: Path) -> None:
    pts = shapely.points(df["lon"].to_numpy(), df["lat"].to_numpy())
    cols = {c: pa.array(df[c].to_numpy() if df[c].dtype != object else df[c].astype(str).to_numpy())
            for c in df.columns if c not in ("lon", "lat")}
    cols["geometry"] = pa.array(shapely.to_wkb(pts), pa.binary())
    lon, lat = df["lon"].to_numpy(), df["lat"].to_numpy()
    cols["bbox"] = _bbox_struct(lon, lat, lon, lat)
    table = pa.table(cols).replace_schema_metadata({b"geo": json.dumps(_geo_metadata(["Point"], {})).encode()})
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")
