"""Public transport quality after the ARE method (methodology §3.3; ARE 2022).

Mode ``polygons`` reads the published ARE quality-class polygons. Mode ``gtfs`` recomputes stop
categories from the timetable and adds a continuous score inside each distance band.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from ..grid import to_lv95

# ARE stop category by service interval (minutes) and mode group
INTERVAL_EDGES = [5, 10, 20, 40, 60.0001]
CATEGORY = {
    "rail_node": ["I", "I", "II", "III", "IV"],
    "rail_line": ["I", "II", "III", "IV", "V"],
    "road": ["II", "III", "IV", "V", "V"],
    "cable": ["V", "V", "V", "V", "V"],
}
# quality class by category and distance band (air-line, metres)
BANDS = [300, 500, 750, 1000]
CLASS_MATRIX = {
    "I": ["A", "A", "B", "C"],
    "II": ["A", "B", "C", "D"],
    "III": ["B", "C", "D", None],
    "IV": ["C", "D", None, None],
    "V": ["D", None, None, None],
}
CAT_ORDER = ["I", "II", "III", "IV", "V"]


def route_group(route_type: int) -> str:
    rt = int(route_type)
    if rt == 2 or 100 <= rt <= 117:
        return "rail"
    if rt in (6, 7) or 1300 <= rt <= 1499:
        return "cable"
    return "road"


def category_for(interval_min: float, group: str) -> str | None:
    for i, edge in enumerate(INTERVAL_EDGES):
        if interval_min < edge:
            return CATEGORY[group][i]
    return None


def best_category(cats) -> str | None:
    cats = [c for c in cats if c]
    return min(cats, key=CAT_ORDER.index) if cats else None


def class_at(category: str, d: float) -> str | None:
    for i, edge in enumerate(BANDS):
        if (d < edge) if i == 0 else (d <= edge):
            return CLASS_MATRIX[category][i]
    return None


def continuous_score(category: str, d: float, scores: dict) -> float:
    """Linear interpolation from the band's class score to the next band's class score."""
    lo = 0.0
    for i, hi in enumerate(BANDS):
        if d <= hi:
            here = scores.get(CLASS_MATRIX[category][i] or "none", 0.0)
            nxt_cls = CLASS_MATRIX[category][i + 1] if i + 1 < len(BANDS) else None
            nxt = scores.get(nxt_cls or "none", 0.0)
            t = (d - lo) / (hi - lo)
            return float(here - (here - nxt) * t)
        lo = hi
    return 0.0


# ------------------------------------------------------------------ GTFS
def _active_services(cal: pd.DataFrame | None, cdates: pd.DataFrame | None, day: date) -> set[str]:
    active: set[str] = set()
    ymd = day.strftime("%Y%m%d")
    if cal is not None and len(cal):
        wd = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][day.weekday()]
        m = (cal[wd] == "1") & (cal["start_date"] <= ymd) & (cal["end_date"] >= ymd)
        active |= set(cal.loc[m, "service_id"])
    if cdates is not None and len(cdates):
        d = cdates[cdates["date"] == ymd]
        active |= set(d.loc[d["exception_type"] == "1", "service_id"])
        active -= set(d.loc[d["exception_type"] == "2", "service_id"])
    return active


def _minutes(hms: pd.Series) -> np.ndarray:
    parts = hms.str.split(":", expand=True).astype(float)
    return (parts[0] * 60 + parts[1] + parts[2] / 60).to_numpy()


def stop_categories(tables: dict[str, pd.DataFrame], day: date, window=("06:00", "20:00")) -> pd.DataFrame:
    """ARE category per stop (grouped by parent_station) for a reference day."""
    stops, routes, trips, st = tables["stops"], tables["routes"], tables["trips"], tables["stop_times"]
    services = _active_services(tables.get("calendar"), tables.get("calendar_dates"), day)
    trips = trips[trips["service_id"].isin(services)].merge(routes[["route_id", "route_type"]], on="route_id")
    trips["group"] = trips["route_type"].map(route_group)
    st = st.merge(trips[["trip_id", "route_id", "group"] + (["direction_id"] if "direction_id" in trips else [])],
                  on="trip_id")
    st["seq"] = st["stop_sequence"].astype(int)
    last_seq = st.groupby("trip_id")["seq"].transform("max")
    st["terminal"] = (st["seq"] == st.groupby("trip_id")["seq"].transform("min")) | (st["seq"] == last_seq)
    t = _minutes(st["departure_time"].where(st["departure_time"] != "", st["arrival_time"]))
    w0, w1 = (int(x[:2]) * 60 + int(x[3:5]) for x in window)
    st = st[(t >= w0) & (t < w1) & (st["seq"] != last_seq)]
    parent = stops.set_index("stop_id")["parent_station"] if "parent_station" in stops else None
    key = st["stop_id"].map(parent).where(lambda s: s.fillna("") != "", st["stop_id"]) if parent is not None else st["stop_id"]
    st = st.assign(stop_key=key.to_numpy())
    window_min = float(w1 - w0)
    rows = []
    for (sk, group), g in st.groupby(["stop_key", "group"]):
        n = len(g)
        one_direction = "direction_id" in g and g["direction_id"].nunique() == 1
        per_dir = n if (one_direction or g["terminal"].all()) else n / 2
        interval = window_min / per_dir if per_dir else np.inf
        sub = "cable" if group == "cable" else group
        if group == "rail":
            sub = "rail_node" if g["route_id"].nunique() > 1 else "rail_line"
        rows.append({"stop_key": sk, "group": sub, "interval_min": interval,
                     "category": category_for(interval, sub)})
    cats = pd.DataFrame(rows, columns=["stop_key", "group", "interval_min", "category"])
    best = cats.dropna(subset=["category"]).groupby("stop_key").agg(
        category=("category", best_category), interval_min=("interval_min", "min")).reset_index()
    pos = stops.set_index("stop_id")[["stop_lat", "stop_lon"]].astype(float)
    best = best.join(pos, on="stop_key", how="inner")
    return best


def _reference_day(params: dict) -> date:
    if params.get("reference_date"):
        return datetime.strptime(str(params["reference_date"]), "%Y-%m-%d").date()
    d = date.today()
    while d.weekday() != 1:  # next Tuesday
        d += timedelta(days=1)
    return d


def are_classes(ctx, params: dict) -> pd.DataFrame:
    scores = {"A": 10.0, "B": 7.5, "C": 5.0, "D": 2.5, "none": 0.0} | params.get("class_scores", {})
    mode = params.get("mode", "polygons")
    u = ctx.universe
    lon, lat = u["lon"].to_numpy(), u["lat"].to_numpy()
    if mode == "polygons":
        sid = params.get("source", "are_oev_gueteklassen")
        field = params.get("class_field", "klasse")
        polys = gpd.read_parquet(ctx.staged(sid))
        pts = gpd.GeoDataFrame({"i": np.arange(len(u))}, geometry=gpd.points_from_xy(lon, lat), crs="EPSG:4326")
        hit = gpd.sjoin(pts, polys[[field, "geometry"]].to_crs("EPSG:4326"), predicate="within")
        hit["s"] = hit[field].astype(str).str.strip().str.upper().map(scores).fillna(0.0)
        best = hit.groupby("i")["s"].max()
        raw = np.zeros(len(u))
        raw[best.index.to_numpy()] = best.to_numpy()
        return pd.DataFrame({"cell_id": ctx.cells, "raw": raw})
    if mode != "gtfs":
        raise ValueError(f"public_transport: unknown mode {mode!r}")
    sid = params.get("gtfs_source", "ch_gtfs")
    tables = {}
    for name in ("stops", "routes", "trips", "stop_times", "calendar", "calendar_dates"):
        if f"{name}.parquet" in ctx.staged_names(sid):
            tables[name] = pd.read_parquet(ctx.staged(sid, f"{name}.parquet"))
    cats = stop_categories(tables, _reference_day(params), tuple(params.get("window", ["06:00", "20:00"])))
    raw = np.zeros(len(u))
    if len(cats):
        sx, sy = to_lv95(cats["stop_lon"].to_numpy(), cats["stop_lat"].to_numpy())
        cx, cy = to_lv95(lon, lat)
        tree = cKDTree(np.column_stack([sx, sy]))
        near = tree.query_ball_point(np.column_stack([cx, cy]), r=BANDS[-1])
        cat_arr = cats["category"].to_numpy()
        for i, members in enumerate(near):
            if not members:
                continue
            d = np.hypot(sx[members] - cx[i], sy[members] - cy[i])
            raw[i] = max(continuous_score(cat_arr[m], float(dd), scores) for m, dd in zip(members, d, strict=False))
    return pd.DataFrame({"cell_id": ctx.cells, "raw": raw})
