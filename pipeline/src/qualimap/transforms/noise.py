"""Noise: sonBASE road and rail (day/night) plus FOCA aerodrome contours (methodology §3.1)."""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd

from . import energetic_sum

DEFAULT_SOURCES = {
    "road_day": "foen_sonbase_road_day",
    "road_night": "foen_sonbase_road_night",
    "rail_day": "foen_sonbase_rail_day",
    "rail_night": "foen_sonbase_rail_night",
    "aircraft": "foca_aerodrome_noise",
}


def _sample(ctx, sid, lon, lat):
    if not sid or not ctx.has(sid):
        return np.full(len(lon), np.nan)
    return ctx.sample_raster(sid, lon, lat)


def _aircraft(ctx, sid, lon, lat, params, period: str):
    if not sid or not ctx.has(sid) or len(lon) == 0:
        return np.full(len(lon), np.nan)
    contours = gpd.read_parquet(ctx.staged(sid))
    level_field = params.get("aircraft_level_field", "level")
    period_field = params.get("aircraft_period_field")
    if period_field and period_field in contours:
        contours = contours[contours[period_field].astype(str).str.lower().str.startswith(period[0])]
    if contours.empty:
        return np.full(len(lon), np.nan)
    pts = gpd.GeoDataFrame({"i": np.arange(len(lon))}, geometry=gpd.points_from_xy(lon, lat), crs="EPSG:4326")
    hit = gpd.sjoin(pts, contours[[level_field, "geometry"]].to_crs("EPSG:4326"), predicate="within")
    best = hit.groupby("i")[level_field].max()
    out = np.full(len(lon), np.nan)
    out[best.index.to_numpy()] = best.to_numpy(float)
    return out


def _levels(ctx, lon, lat, params):
    s = DEFAULT_SOURCES | params.get("sources", {})
    off = float(params.get("night_offset_db", 10))
    bg = float(params.get("background_db", 35))
    day = energetic_sum([_sample(ctx, s["road_day"], lon, lat), _sample(ctx, s["rail_day"], lon, lat),
                         _aircraft(ctx, s["aircraft"], lon, lat, params, "day")])
    night = energetic_sum([_sample(ctx, s["road_night"], lon, lat), _sample(ctx, s["rail_night"], lon, lat),
                           _aircraft(ctx, s["aircraft"], lon, lat, params, "night")])
    day = np.where(np.isnan(day), bg, day)
    night = np.where(np.isnan(night), bg - off, night)
    return np.maximum(day, night + off)


def effective_level(ctx, params: dict) -> pd.DataFrame:
    """L_eff = max(L_day, L_night + offset), population-weighted energetic mean per cell."""
    u = ctx.universe
    raw = _levels(ctx, u["lon"].to_numpy(), u["lat"].to_numpy(), params)
    b = ctx.buildings
    if len(b) and b["pop"].sum() > 0:
        lb = _levels(ctx, b["lon"].to_numpy(), b["lat"].to_numpy(), params)
        w = b["pop"].to_numpy(float)
        e = pd.DataFrame({"cell_id": b["cell_id"].to_numpy(np.uint64), "num": w * 10 ** (lb / 10), "w": w})
        g = e.groupby("cell_id")[["num", "w"]].sum()
        g = g[g["w"] > 0]
        idx = ctx.index_of(g.index.to_numpy(np.uint64))
        ok = idx >= 0
        raw[idx[ok]] = 10 * np.log10(g["num"].to_numpy()[ok] / g["w"].to_numpy()[ok])
    return pd.DataFrame({"cell_id": ctx.cells, "raw": raw})
