"""Shared context handed to every transform: grid universe, buildings, staged inputs, samplers."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import numpy as np
import pandas as pd

from .catalog import Catalog
from .staging import staged_names, staged_path
from .state import StateStore
from .store import Store


@dataclass
class Context:
    catalog: Catalog
    store: Store
    state: StateStore
    res: int
    log: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ grid
    @cached_property
    def universe(self) -> pd.DataFrame:
        key = f"cells/universe_r{self.res}.parquet"
        if not self.store.exists(key):
            raise RuntimeError(f"cell universe missing ({key}); run `qm cells build`")
        return pd.read_parquet(self.store.local_path(key))

    @cached_property
    def cells(self) -> np.ndarray:
        return self.universe["cell_id"].to_numpy(np.uint64)

    @cached_property
    def buildings(self) -> pd.DataFrame:
        """Residential buildings with dasymetric population (see :mod:`qualimap.population`)."""
        key = f"cells/buildings_r{self.res}.parquet"
        if not self.store.exists(key):
            return pd.DataFrame(columns=["egid", "lon", "lat", "cell_id", "commune_bfs", "pop"])
        return pd.read_parquet(self.store.local_path(key))

    @cached_property
    def cell_pop(self) -> np.ndarray:
        """Population per universe cell (aligned with :attr:`cells`)."""
        if "pop" in self.universe.columns:
            return self.universe["pop"].to_numpy(float)
        return np.zeros(len(self.universe))

    def index_of(self, cell_ids: np.ndarray) -> np.ndarray:
        """Row index in the universe for each cell id (-1 if outside)."""
        cells = self.cells
        pos = np.searchsorted(cells, np.asarray(cell_ids, np.uint64))
        pos = np.clip(pos, 0, len(cells) - 1)
        ok = cells[pos] == np.asarray(cell_ids, np.uint64)
        return np.where(ok, pos, -1)

    # ------------------------------------------------------------------ inputs
    def staged(self, sid: str, name: str | None = None) -> Path:
        return staged_path(self.store, self.state, sid, name)

    def staged_names(self, sid: str) -> list[str]:
        return staged_names(self.state, sid)

    def has(self, sid: str) -> bool:
        return bool(self.state.staged_record(sid))

    def sample_raster(self, sid: str, lon: np.ndarray, lat: np.ndarray, name: str | None = None,
                      band: int = 1) -> np.ndarray:
        """Raster values at WGS84 points (NaN for no-data or outside the raster)."""
        import rasterio
        from pyproj import Transformer

        path = self.staged(sid, name)
        with rasterio.open(path) as ds:
            tr = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True)
            x, y = tr.transform(np.asarray(lon, float), np.asarray(lat, float))
            rows, cols = rasterio.transform.rowcol(ds.transform, x, y)
            rows, cols = np.asarray(rows), np.asarray(cols)
            inside = (rows >= 0) & (rows < ds.height) & (cols >= 0) & (cols < ds.width)
            out = np.full(len(x), np.nan)
            if inside.any():
                arr = ds.read(band, masked=True)
                vals = arr[rows[inside], cols[inside]]
                out[inside] = np.ma.filled(vals.astype(float), np.nan)
            return out

    def zonal_mean(self, sid: str, name: str | None = None) -> np.ndarray:
        """Area-weighted mean of a raster over each universe cell polygon (needs exactextract)."""
        import geopandas as gpd
        from exactextract import exact_extract

        from .grid import cell_polygons

        polys = gpd.GeoDataFrame({"i": np.arange(len(self.cells))},
                                 geometry=cell_polygons(self.cells), crs="EPSG:4326")
        res = exact_extract(str(self.staged(sid, name)), polys, "mean", output="pandas")
        return res["mean"].to_numpy(float)

    def cell_values(self, sid: str, name: str | None = None, method: str = "centroid") -> np.ndarray:
        if method == "zonal":
            return self.zonal_mean(sid, name)
        return self.sample_raster(sid, self.universe["lon"].to_numpy(), self.universe["lat"].to_numpy(), name)

    def note(self, msg: str) -> None:
        self.log.append(msg)
