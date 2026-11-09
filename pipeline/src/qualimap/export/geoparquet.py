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


def tile_resolution(res: int) -> int:
    """Resolution of the A5 cells used as viewer tiles and Parquet row groups (≈1,024 cells each)."""
    return max(res - 5, 0)


def write_cells(df: pd.DataFrame, path: Path, res: int, release_meta: dict) -> dict:
    """One row group per tile cell at ``tile_resolution(res)`` (res 16 → one per res-11 parent).

    Rows are sorted by ``cell_id``; because A5 descendants are contiguous on the curve, each
    run of equal tile parents is exactly one tile's cells.
    """
    df = df.sort_values("cell_id", kind="stable").reset_index(drop=True)
    tres = tile_resolution(res)
    cells = df["cell_id"].to_numpy(np.uint64)
    tiles = cells if tres == res else grid.parents(cells, tres)
    table = cells_to_arrow(df)
    meta = _geo_metadata(["Point"], {})
    schema = table.schema.with_metadata({
        b"geo": json.dumps(meta).encode(),
        b"qualimap": json.dumps(release_meta | {"a5_resolution": res, "tile_resolution": tres}).encode(),
    })
    table = table.replace_schema_metadata(schema.metadata)
    path.parent.mkdir(parents=True, exist_ok=True)
    bounds = np.flatnonzero(np.diff(tiles)) + 1
    starts = np.concatenate([[0], bounds]).astype(int)
    ends = np.concatenate([bounds, [len(df)]]).astype(int)
    with pq.ParquetWriter(path, schema, compression="zstd", compression_level=9,
                          write_page_index=True, write_statistics=True) as w:
        for s, e in zip(starts, ends, strict=False):
            w.write_table(table.slice(int(s), int(e - s)))
    return {"rows": len(df), "row_groups": len(starts), "tile_resolution": tres}


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


# ---------------------------------------------------------------------------- GeoArrow IPC
def _coords_type() -> pa.DataType:
    return pa.list_(pa.field("xy", pa.float64()), 2)


def _geoarrow_field(name: str, ext: str, typ: pa.DataType) -> pa.Field:
    meta = {b"ARROW:extension:name": ext.encode(), b"ARROW:extension:metadata": b'{"crs": "OGC:CRS84"}'}
    return pa.field(name, typ, metadata=meta)


def multipolygon_array(geoms) -> tuple[pa.Array, pa.DataType]:
    """Interleaved geoarrow.multipolygon: list<polygons: list<rings: list<vertices: fixed_size_list<xy, 2>>>>."""
    xy, ring_off, poly_off, geom_off = [], [0], [0], [0]
    for g in geoms:
        parts = list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
        for p in parts:
            for ring in [p.exterior, *p.interiors]:
                c = np.asarray(ring.coords)[:, :2]
                xy.append(c)
                ring_off.append(ring_off[-1] + len(c))
            poly_off.append(len(ring_off) - 1)
        geom_off.append(len(poly_off) - 1)
    coords = np.concatenate(xy).ravel() if xy else np.empty(0)
    verts = pa.FixedSizeListArray.from_arrays(pa.array(coords, pa.float64()), 2)
    rings = pa.ListArray.from_arrays(pa.array(ring_off, pa.int32()), verts)
    polys = pa.ListArray.from_arrays(pa.array(poly_off, pa.int32()), rings)
    multi = pa.ListArray.from_arrays(pa.array(geom_off, pa.int32()), polys)
    return multi, multi.type


def write_geoarrow_polygons(gdf, path: Path) -> None:
    """Arrow IPC file with an interleaved GeoArrow geometry column, for @geoarrow/deck.gl-geoarrow."""
    import pyarrow.ipc as ipc

    arr, typ = multipolygon_array(gdf.geometry.to_numpy())
    fields = [pa.field(c, pa.array(gdf[c].to_numpy()).type) for c in gdf.columns if c != "geometry"]
    cols = [pa.array(gdf[c].to_numpy()) for c in gdf.columns if c != "geometry"]
    schema = pa.schema(fields + [_geoarrow_field("geometry", "geoarrow.multipolygon", typ)])
    table = pa.Table.from_arrays(cols + [arr], schema=schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ipc.new_file(str(path), schema) as w:
        w.write_table(table)


def write_geoarrow_points(df: pd.DataFrame, path: Path) -> None:
    import pyarrow.ipc as ipc

    coords = np.column_stack([df["lon"].to_numpy(float), df["lat"].to_numpy(float)]).ravel()
    pts = pa.FixedSizeListArray.from_arrays(pa.array(coords, pa.float64()), 2)
    keep = [c for c in df.columns if c not in ("lon", "lat")]
    cols = [pa.array(df[c].to_numpy() if df[c].dtype != object else df[c].astype(str).to_numpy()) for c in keep]
    schema = pa.schema([pa.field(c, a.type) for c, a in zip(keep, cols, strict=False)]
                       + [_geoarrow_field("geometry", "geoarrow.point", pts.type)])
    table = pa.Table.from_arrays(cols + [pts], schema=schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ipc.new_file(str(path), schema) as w:
        w.write_table(table)
