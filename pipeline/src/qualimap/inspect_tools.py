"""``qm inspect``: values and lineage for one cell or commune."""

from __future__ import annotations

import a5
import numpy as np
import pandas as pd

from .compute import load_indicator


def parse_cell(target: str, res: int) -> int:
    if "," in target:
        lon, lat = (float(v) for v in target.split(","))
        return int(a5.lonlat_to_cell((lon, lat), res))
    return int(target, 16) if target.lower().startswith("0x") else int(target)


def inspect_cell(env, target: str, res: int | None = None) -> dict:
    res = res or env.catalog.base_resolution
    cell = parse_cell(target, res)
    cell_res = a5.get_resolution(cell)
    base = env.catalog.base_resolution
    out = {"cell_id": cell, "hex": hex(cell), "resolution": cell_res,
           "centre": list(a5.cell_to_lonlat(cell)), "indicators": {}}
    if cell_res != base:
        out["note"] = f"values below are for res-{base} descendants; showing the first"
        cell = int(a5.cell_to_children(cell, base)[0]) if cell_res < base else int(a5.cell_to_parent(cell, base))
    key = f"cells/universe_r{base}.parquet"
    if env.data.exists(key):
        uni = pd.read_parquet(env.data.local_path(key))
        row = uni[uni["cell_id"] == np.uint64(cell)]
        if len(row):
            r = row.iloc[0]
            out |= {"commune_bfs": int(r["commune_bfs"]), "pop": float(r.get("pop", 0)),
                    "in_share": float(r["in_share"])}
    for iid, ind in env.catalog.indicators.items():
        df = load_indicator(env.data, env.state, iid)
        rec = env.state.indicator_record(iid) or {}
        if df is None:
            out["indicators"][iid] = None
            continue
        row = df[df["cell_id"] == np.uint64(cell)]
        if len(row):
            r = row.iloc[0]
            out["indicators"][iid] = {"raw": None if np.isnan(r["raw"]) else float(r["raw"]),
                                      "score": None if np.isnan(r["score"]) else round(float(r["score"]), 2),
                                      "q": int(r["q"]), "unit": ind.raw.get("unit"),
                                      "inputs": {s: (env.state.staged_record(s) or {}).get("raw_sha256", "")[:12]
                                                 for s in ind.inputs},
                                      "built": rec.get("computed_at")}
    return out


def inspect_commune(env, target: str) -> dict:
    from . import releases

    latest = releases.latest(env.pub)
    if not latest:
        raise RuntimeError("no published release")
    v = latest["version"]
    df = pd.read_parquet(env.pub.local_path(f"releases/{v}/parquet/communes.parquet"))
    if target.isdigit():
        row = df[df["commune_bfs"] == int(target)]
    else:
        row = df[df["name"].str.lower() == target.lower()] if "name" in df else df.iloc[0:0]
    if row.empty:
        raise RuntimeError(f"commune {target!r} not in release {v}")
    r = row.iloc[0]
    out = {"release": v, "commune_bfs": int(r["commune_bfs"]), "name": r.get("name"), "pop": float(r["pop"]),
           "indicators": {}}
    for c in [c for c in df.columns if c.startswith("q_")]:
        valid = df[c] != 255
        rank = int((df.loc[valid, c] > r[c]).sum()) + 1 if r[c] != 255 else None
        out["indicators"][c[2:]] = {"score": None if r[c] == 255 else r[c] / 25, "rank": rank,
                                    "of": int(valid.sum()), "raw": float(r.get(f"raw_{c[2:]}", np.nan))}
    return out
