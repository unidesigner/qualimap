"""Assemble the per-resolution cell tables from the universe and computed indicators."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .. import grid, normalize
from ..catalog import Catalog
from ..compute import load_indicator
from ..state import StateStore
from ..store import Store


def base_table(catalog: Catalog, store: Store, state: StateStore, res: int) -> tuple[pd.DataFrame, list[str]]:
    uni = pd.read_parquet(store.local_path(f"cells/universe_r{res}.parquet"))
    df = uni.copy()
    if "pop" not in df:
        df["pop"] = np.float32(0)
    df["flags"] = np.zeros(len(df), np.uint32)
    present = []
    for ind in catalog.indicators.values():
        t = load_indicator(store, state, ind.id)
        if t is None:
            continue
        t = df[["cell_id"]].merge(t, on="cell_id", how="left")
        df[f"score_{ind.id}"] = t["score"].to_numpy(np.float32)
        df[f"raw_{ind.id}"] = t["raw"].to_numpy(np.float32)
        df["flags"] |= t["flags"].fillna(0).to_numpy(np.uint32)
        present.append(ind.id)
    return df, present


def rollup(catalog: Catalog, base: pd.DataFrame, present: list[str], base_res: int, res: int) -> pd.DataFrame:
    """Aggregate the base table to ``res`` (always from the base resolution, SPEC §3.6)."""
    if res == base_res:
        return base
    pcol = f"parent_r{res}"
    if pcol not in base:
        base = base.assign(**{pcol: grid.parents(base["cell_id"].to_numpy(np.uint64), res)})
    pop = base["pop"].to_numpy(float)
    area = base["in_share"].to_numpy(float) if "in_share" in base else np.ones(len(base))
    out = None
    for ind_id in present:
        ind = catalog.indicator(ind_id)
        agg = ind.aggregation
        weight = area if agg == "area_mean" else (pop if pop.sum() > 0 else area)
        energetic = [f"raw_{ind_id}"] if agg == "pop_weighted_energetic_mean" else []
        maximum = [f"raw_{ind_id}", f"score_{ind_id}"] if agg == "max" else []
        part = grid.weighted_rollup(base, pcol, [f"raw_{ind_id}", f"score_{ind_id}"], weight,
                                    energetic=energetic, maximum=maximum)
        out = part if out is None else out.merge(part, on="cell_id")
    keys, codes = np.unique(base[pcol].to_numpy(np.uint64), return_inverse=True)
    meta = pd.DataFrame({"cell_id": keys})
    meta["pop"] = np.bincount(codes, weights=pop, minlength=len(keys)).astype(np.float32)
    meta["in_share"] = (np.bincount(codes, weights=area, minlength=len(keys)) /
                        np.bincount(codes, minlength=len(keys))).astype(np.float32)
    flags = pd.Series(base["flags"].to_numpy(np.uint32)).groupby(codes).agg(lambda s: np.bitwise_or.reduce(s.to_numpy()))
    meta["flags"] = flags.to_numpy(np.uint32)
    # majority commune by population (area when unpopulated)
    w = np.where(pop > 0, pop, area * 1e-6)
    cm = pd.DataFrame({"p": codes, "c": base["commune_bfs"].to_numpy(), "w": w})
    best = cm.groupby(["p", "c"])["w"].sum().reset_index().sort_values("w").drop_duplicates("p", keep="last")
    meta["commune_bfs"] = best.set_index("p").reindex(range(len(keys)))["c"].fillna(0).to_numpy(np.uint16)
    lon, lat = grid.cell_centroids(keys)
    meta["lon"], meta["lat"] = lon, lat
    for r in range(grid.ROLLUP_FLOOR, res):
        meta[f"parent_r{r}"] = grid.parents(keys, r)
    return meta if out is None else meta.merge(out, on="cell_id")


def finalize(df: pd.DataFrame, present: list[str], catalog: Catalog) -> pd.DataFrame:
    """Quantise scores and order columns for publication."""
    out = pd.DataFrame({"cell_id": df["cell_id"].to_numpy(np.uint64)})
    out["lon"] = df["lon"].to_numpy(float)
    out["lat"] = df["lat"].to_numpy(float)
    out["commune_bfs"] = df["commune_bfs"].fillna(0).to_numpy(np.uint16)
    out["pop"] = df["pop"].to_numpy(np.float32)
    for i in present:
        out[f"q_{i}"] = normalize.quantize(df[f"score_{i}"].to_numpy(float), catalog.quant_scale, catalog.nodata)
    for i in present:
        out[f"raw_{i}"] = df[f"raw_{i}"].to_numpy(np.float32)
    out["flags"] = df["flags"].to_numpy(np.uint32)
    if "parent_r11" in df:
        out["parent_r11"] = df["parent_r11"].to_numpy(np.uint64)
    return out.sort_values("cell_id", kind="stable").reset_index(drop=True)


def commune_table(catalog: Catalog, base: pd.DataFrame, present: list[str]) -> pd.DataFrame:
    pop = base["pop"].to_numpy(float)
    area = base["in_share"].to_numpy(float) if "in_share" in base else np.ones(len(base))
    b = base.assign(_c=base["commune_bfs"].astype(np.uint64))
    out = None
    for ind_id in present:
        agg = catalog.indicator(ind_id).aggregation
        weight = area if agg == "area_mean" else (pop if pop.sum() > 0 else area)
        part = grid.weighted_rollup(b, "_c", [f"raw_{ind_id}", f"score_{ind_id}"], weight,
                                    energetic=[f"raw_{ind_id}"] if agg == "pop_weighted_energetic_mean" else [],
                                    maximum=[f"raw_{ind_id}", f"score_{ind_id}"] if agg == "max" else [])
        out = part if out is None else out.merge(part, on="cell_id")
    keys, codes = np.unique(b["_c"].to_numpy(np.uint64), return_inverse=True)
    meta = pd.DataFrame({"cell_id": keys, "pop": np.bincount(codes, weights=pop, minlength=len(keys))})
    res = meta if out is None else meta.merge(out, on="cell_id")
    res = res.rename(columns={"cell_id": "commune_bfs"})
    res["commune_bfs"] = res["commune_bfs"].astype(np.uint16)
    return res
