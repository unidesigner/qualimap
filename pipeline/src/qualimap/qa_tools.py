"""``qm qa run --compare-neighbourhoods``: compute an indicator with each neighbourhood method."""

from __future__ import annotations

import copy

import numpy as np
import pandas as pd

from .catalog import Indicator
from .transforms import load_transform

METHODS = ("spherical_cap", "grid_disk", "grid_disk_vertex")


def compare_neighbourhoods(env, indicator_id: str) -> list[dict]:
    ind = env.catalog.indicator(indicator_id)
    if "neighbourhood" not in ind.params:
        raise ValueError(f"{indicator_id} has no neighbourhood parameter")
    ctx = env.context()
    fn = load_transform(ind.transform["fn"])
    results = {}
    for m in METHODS:
        params = copy.deepcopy(ind.params)
        params["neighbourhood"] = {k: v for k, v in params["neighbourhood"].items() if k != "k"} | {"method": m}
        results[m] = fn(ctx, params)["raw"].to_numpy(float)
    base = results["spherical_cap"]
    pop = ctx.cell_pop
    communes = ctx.universe["commune_bfs"].to_numpy()
    rows = []
    for m in METHODS:
        v = results[m]
        ok = ~np.isnan(v) & ~np.isnan(base)
        d = v[ok] - base[ok]
        cm = pd.DataFrame({"c": communes[ok], "d": d, "w": np.maximum(pop[ok], 1e-9)})
        per_commune = cm.groupby("c").apply(lambda g: np.average(g["d"], weights=g["w"]), include_groups=False)
        rows.append({"method": m, "cells": int(ok.sum()),
                     "mean": float(np.nanmean(v)), "mean_abs_diff_vs_cap": float(np.abs(d).mean()) if len(d) else 0.0,
                     "p95_abs_diff_vs_cap": float(np.percentile(np.abs(d), 95)) if len(d) else 0.0,
                     "max_commune_abs_diff": float(per_commune.abs().max()) if len(per_commune) else 0.0})
    return rows


__all__ = ["compare_neighbourhoods", "Indicator"]
