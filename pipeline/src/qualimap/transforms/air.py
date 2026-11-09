"""Air quality: annual means of NO₂, PM10, PM2.5 relative to the OAPC limits (methodology §3.2).

``raw`` is the largest concentration/limit ratio (the limiting pollutant). Normalising it with
``linear good=0.5 bad=1.5`` equals the minimum of the per-pollutant scores.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import FLAG_LIMIT_EXCEEDED

DEFAULT_SOURCES = {"no2": "foen_no2", "pm10": "foen_pm10", "pm25": "foen_pm25"}


def limiting_pollutant(ctx, params: dict) -> pd.DataFrame:
    profiles = params.get("profiles", {"LRV_current": {"no2": 30, "pm10": 20, "pm25": 10}})
    limits = profiles[params.get("limits_profile", "LRV_current")]
    sources = DEFAULT_SOURCES | params.get("sources", {})
    u = ctx.universe
    ratios = []
    flags = np.zeros(len(u), dtype=np.uint32)
    b = ctx.buildings
    for pol, limit in limits.items():
        sid = sources.get(pol)
        if not sid or not ctx.has(sid):
            ctx.note(f"air: {pol} source {sid} not staged; skipped")
            continue
        c = ctx.cell_values(sid, method=params.get("sampling", "centroid"))
        if len(b) and b["pop"].sum() > 0:
            cb = ctx.sample_raster(sid, b["lon"].to_numpy(), b["lat"].to_numpy())
            df = pd.DataFrame({"cell_id": b["cell_id"].to_numpy(np.uint64), "num": cb * b["pop"], "w": b["pop"]})
            df = df.dropna()
            g = df.groupby("cell_id")[["num", "w"]].sum()
            g = g[g["w"] > 0]
            idx = ctx.index_of(g.index.to_numpy(np.uint64))
            ok = idx >= 0
            c[idx[ok]] = (g["num"] / g["w"]).to_numpy()[ok]
        r = c / float(limit)
        flags |= np.where(r > 1, FLAG_LIMIT_EXCEEDED, 0).astype(np.uint32)
        ratios.append(r)
    if not ratios:
        raw = np.full(len(u), np.nan)
    else:
        stack = np.vstack(ratios)
        raw = np.where(np.isnan(stack).all(axis=0), np.nan, np.nanmax(np.where(np.isnan(stack), -np.inf, stack), axis=0))
    return pd.DataFrame({"cell_id": ctx.cells, "raw": raw, "flags": flags})
