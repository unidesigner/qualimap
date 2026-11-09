"""Stage 4: compute indicators with input-hash caching (SPEC §5.2)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import a5
import numpy as np
import pandas as pd

from . import normalize, qa
from .catalog import Catalog, Indicator
from .context import Context
from .state import StateStore, stable_hash, utcnow
from .store import Store
from .transforms import load_transform


def inputs_hash(catalog: Catalog, ind: Indicator, state: StateStore, store: Store, res: int) -> str:
    inputs = {}
    for sid in ind.inputs:
        rec = state.staged_record(sid)
        inputs[sid] = rec.get("raw_sha256") if rec else None
    cells_rec = store.get_json(f"state/cells_r{res}.json", {}) or {}
    universe = [cells_rec.get("ids_sha256"), cells_rec.get("inputs")]
    return stable_hash({
        "inputs": inputs,
        "transform": ind.transform,
        "normalize": ind.normalize,
        "aggregation": ind.aggregation,
        "level": ind.level,
        "a5": getattr(a5, "__version__", None) or _a5_version(),
        "res": res,
        "universe": universe,
        "quant": [catalog.quant_scale, catalog.nodata],
    })


def _a5_version() -> str:
    from importlib.metadata import version

    try:
        return version("pya5")
    except Exception:
        return "unknown"


def is_dirty(catalog: Catalog, ind: Indicator, state: StateStore, store: Store, res: int) -> bool:
    rec = state.indicator_record(ind.id)
    return not rec or rec.get("hash") != inputs_hash(catalog, ind, state, store, res) or rec.get("res") != res


def compute_indicator(ctx: Context, ind: Indicator, force: bool = False) -> dict:
    catalog, state, store, res = ctx.catalog, ctx.state, ctx.store, ctx.res
    h = inputs_hash(catalog, ind, state, store, res)
    prev = state.indicator_record(ind.id)
    if prev and prev.get("hash") == h and prev.get("res") == res and not force:
        return prev | {"unchanged": True}
    missing = [s for s in ind.inputs if catalog.source(s).method != "none" and not state.staged_record(s)]
    required = ind.params.get("required_inputs", ind.inputs[:1])
    if any(s in missing for s in required):
        raise RuntimeError(f"{ind.id}: required inputs not staged: {[s for s in required if s in missing]}")
    fn = load_transform(ind.transform["fn"])
    out = fn(ctx, ind.params)
    u = ctx.universe
    if ind.level == "commune":
        df = _commune_to_cells(ctx, ind, out)
    else:
        df = u[["cell_id"]].merge(out, on="cell_id", how="left")
        weights = ctx.cell_pop
        df["score"] = normalize.apply(df["raw"].to_numpy(float), ind.normalize, weights)
    if "flags" not in df:
        df["flags"] = 0
    df["flags"] = df["flags"].fillna(0).astype(np.uint32)
    df["q"] = normalize.quantize(df["score"].to_numpy(float), catalog.quant_scale, catalog.nodata)
    df = df[["cell_id", "raw", "score", "q", "flags"]].astype(
        {"cell_id": np.uint64, "raw": np.float32, "score": np.float32})
    checks = qa.run_checks(ind, df, ctx, prev)
    key = f"indicators/{ind.id}/{h[:16]}/r{res}.parquet"
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.parquet"
        df.to_parquet(p, index=False)
        store.put_file(p, key)
    raw = df["raw"].to_numpy(float)
    rec = {
        "indicator_id": ind.id,
        "hash": h,
        "res": res,
        "key": key,
        "computed_at": utcnow().isoformat(),
        "stats": {
            "cells": len(df),
            "valid_cells": int((~np.isnan(raw)).sum()),
            "raw_min": float(np.nanmin(raw)) if (~np.isnan(raw)).any() else None,
            "raw_max": float(np.nanmax(raw)) if (~np.isnan(raw)).any() else None,
            "score_mean": float(np.nanmean(df["score"])) if (~np.isnan(raw)).any() else None,
        },
        "qa": checks,
        "qa_passed": qa.passed(checks),
        "notes": list(ctx.log),
    }
    state.save_indicator_record(ind.id, rec)
    ctx.log.clear()
    return rec


def _commune_to_cells(ctx: Context, ind: Indicator, out: pd.DataFrame) -> pd.DataFrame:
    u = ctx.universe
    pop = pd.Series(ctx.cell_pop).groupby(u["commune_bfs"].to_numpy()).sum()
    out = out.copy()
    out["commune_bfs"] = out["commune_bfs"].astype(int)
    out["w"] = out["commune_bfs"].map(pop).fillna(0.0)
    out["score"] = normalize.apply(out["raw"].to_numpy(float), ind.normalize, out["w"].to_numpy())
    ctx._commune_raw = getattr(ctx, "_commune_raw", {}) | {ind.id: dict(zip(out["commune_bfs"], out["raw"], strict=False))}
    df = u[["cell_id", "commune_bfs"]].astype({"commune_bfs": int}).merge(
        out[["commune_bfs", "raw", "score"]], on="commune_bfs", how="left")
    return df.drop(columns=["commune_bfs"])


def load_indicator(store: Store, state: StateStore, indicator_id: str) -> pd.DataFrame | None:
    rec = state.indicator_record(indicator_id)
    if not rec:
        return None
    return pd.read_parquet(store.local_path(rec["key"]))
