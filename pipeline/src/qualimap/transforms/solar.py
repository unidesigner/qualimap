"""Rooftop solar context layer from Sonnendach.ch (not scored; methodology §3.7)."""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd

from .. import grid


def best_roof_class(ctx, params: dict) -> pd.DataFrame:
    sid = params.get("source", "sfoe_sonnendach")
    roofs = gpd.read_parquet(ctx.staged(sid))
    cls_col = params.get("class_field", "klasse")
    if "gwr_egid" in roofs.columns and len(ctx.buildings):
        b = ctx.buildings[["egid", "cell_id"]].drop_duplicates("egid")
        roofs = roofs.merge(b, left_on="gwr_egid", right_on="egid", how="left")
    missing = roofs["cell_id"].isna() if "cell_id" in roofs else pd.Series(True, index=roofs.index)
    if missing.any():
        c = roofs.loc[missing].geometry.representative_point()
        roofs.loc[missing, "cell_id"] = grid.lonlat_to_cells(c.x.to_numpy(), c.y.to_numpy(), ctx.res)
    roofs["cell_id"] = roofs["cell_id"].astype(np.uint64)
    best = roofs.groupby("cell_id")[cls_col].max()
    raw = np.full(len(ctx.cells), np.nan)
    idx = ctx.index_of(best.index.to_numpy(np.uint64))
    ok = idx >= 0
    raw[idx[ok]] = best.to_numpy(float)[ok]
    return pd.DataFrame({"cell_id": ctx.cells, "raw": raw})
