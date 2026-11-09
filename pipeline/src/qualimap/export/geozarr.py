"""Zarr v3 writers: A5 cell arrays with the DGGS convention, and raster GeoZarr context layers
with the multiscales, geo-proj and spatial conventions (SPEC §6.2, §6.4).

Convention attribute layouts follow the published zarr-conventions repositories as far as they
are documented; the viewer passes explicit metadata to ``ZarrLayer`` so a layout change upstream
does not break rendering. Validate against the convention JSON schemas when they reach V1.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import zarr
from zarr.codecs import ZstdCodec

DGGS_CONVENTION = {
    "uuid": "7b255807-140c-42ca-97f6-7a1cfecdbc38",
    "name": "dggs",
    "schema_url": "https://raw.githubusercontent.com/zarr-conventions/dggs/refs/tags/v1/schema.json",
    "spec_url": "https://github.com/zarr-conventions/dggs/blob/v1/README.md",
    "description": "Discrete Global Grid Systems convention for zarr",
}
MULTISCALES_CONVENTION = {
    "uuid": "d35379db-88df-4056-af3a-620245f8e347",
    "name": "multiscales",
    "spec_url": "https://github.com/zarr-conventions/multiscales",
}
GEO_PROJ_CONVENTION = {"name": "geo-proj", "spec_url": "https://github.com/zarr-conventions/geo-proj"}
SPATIAL_CONVENTION = {"name": "spatial", "spec_url": "https://github.com/zarr-developers/geozarr-spec"}
WGS84 = {"name": "wgs84", "semimajor_axis": 6378137.0, "inverse_flattening": 298.257223563}
CHUNK = 65_536


def _array(group, name: str, data: np.ndarray, chunks: int | tuple = CHUNK, attrs: dict | None = None):
    data = np.ascontiguousarray(data)
    ch = chunks if isinstance(chunks, tuple) else (min(chunks, max(len(data), 1)),) + data.shape[1:]
    arr = group.create_array(name=name, shape=data.shape, dtype=data.dtype, chunks=ch,
                             compressors=[ZstdCodec(level=5)], fill_value=0)
    if data.size:
        arr[...] = data
    if attrs:
        arr.attrs.update(attrs)
    return arr


def write_dggs(tables: dict[int, pd.DataFrame], path: Path, indicators: list[str], release_meta: dict) -> dict:
    root = zarr.open_group(str(path), mode="w", zarr_format=3)
    levels = sorted(tables)
    root.attrs.update({
        "zarr_conventions": [DGGS_CONVENTION, MULTISCALES_CONVENTION],
        "multiscales": {"layout": [{"asset": f"r{r}", "dggs": {"refinement_level": r}} for r in sorted(levels, reverse=True)]},
        "qualimap": release_meta | {"indicators": indicators},
    })
    info = {}
    for res in levels:
        df = tables[res].sort_values("cell_id", kind="stable").reset_index(drop=True)
        g = root.create_group(f"r{res}")
        g.attrs.update({
            "zarr_conventions": [DGGS_CONVENTION],
            "dggs": {"name": "a5", "refinement_level": res, "spatial_dimension": "cells",
                     "coordinate": "cell_ids", "ellipsoid": WGS84},
        })
        dim = {"dimension_names": ["cells"]}
        _array(g, "cell_ids", df["cell_id"].to_numpy(np.uint64))
        _array(g, "pop", df["pop"].to_numpy(np.float32))
        _array(g, "commune_bfs", df["commune_bfs"].to_numpy(np.uint16))
        _array(g, "flags", df["flags"].to_numpy(np.uint32))
        for i in indicators:
            _array(g, f"q_{i}", df[f"q_{i}"].to_numpy(np.uint8), attrs={"_FillValue": 255, "scale": 25})
            _array(g, f"raw_{i}", df[f"raw_{i}"].to_numpy(np.float32))
        if res > 11 and "parent_r11" in df:
            parents = df["parent_r11"].to_numpy(np.uint64)
            keys, first, counts = np.unique(parents, return_index=True, return_counts=True)
            _array(g, "parent_ids", keys)
            _array(g, "parent_index", np.column_stack([first, counts]).astype(np.uint64), chunks=(CHUNK, 2))
        g.attrs["qualimap"] = dim | {"rows": len(df)}
        info[res] = len(df)
    return info


def write_raster(src_path: Path, out_path: Path, boundary_bounds_lv95: tuple | None = None,
                 min_size: int = 256, max_levels: int = 8) -> dict:
    """Raster GeoZarr with a 2× average pyramid in the source CRS."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import from_bounds

    root = zarr.open_group(str(out_path), mode="w", zarr_format=3)
    with rasterio.open(src_path) as ds:
        window = None
        if boundary_bounds_lv95 and ds.crs and ds.crs.to_epsg() == 2056:
            window = from_bounds(*boundary_bounds_lv95, transform=ds.transform).round_offsets().round_lengths()
            window = window.intersection(rasterio.windows.Window(0, 0, ds.width, ds.height))
        base_transform = ds.window_transform(window) if window else ds.transform
        h = int(window.height) if window else ds.height
        w = int(window.width) if window else ds.width
        nodata = ds.nodata
        layout = []
        level = 0
        factor = 1
        while True:
            oh, ow = max(1, h // factor), max(1, w // factor)
            data = ds.read(1, window=window, out_shape=(oh, ow), resampling=Resampling.average,
                           masked=True).astype(np.float32)
            arr = np.ma.filled(data, np.nan)
            t = base_transform @ base_transform.scale(w / ow, h / oh)
            a = root.create_array(name=str(level), shape=arr.shape, dtype=np.float32,
                                  chunks=(min(512, oh), min(512, ow)), compressors=[ZstdCodec(level=5)],
                                  fill_value=np.nan, dimension_names=["y", "x"])
            a[...] = arr
            a.attrs.update({"spatial:transform": [t.a, t.b, t.c, t.d, t.e, t.f],
                            "spatial:shape": [oh, ow], "proj:code": f"EPSG:{ds.crs.to_epsg()}" if ds.crs else None})
            layout.append({"asset": str(level), "transform": {"scale": [float(factor), float(factor)]},
                           "spatial:transform": [t.a, t.b, t.c, t.d, t.e, t.f], "spatial:shape": [oh, ow]})
            level += 1
            factor *= 2
            if max(oh, ow) <= min_size or level >= max_levels:
                break
        root.attrs.update({
            "zarr_conventions": [MULTISCALES_CONVENTION, GEO_PROJ_CONVENTION, SPATIAL_CONVENTION],
            "multiscales": {"layout": layout, "resampling_method": "average"},
            "proj:code": f"EPSG:{ds.crs.to_epsg()}" if ds.crs else None,
            "spatial:dimensions": ["y", "x"],
            "nodata": None if nodata is None else float(nodata),
        })
    return {"levels": level, "layout": layout, "crs": root.attrs.get("proj:code")}
