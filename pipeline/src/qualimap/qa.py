"""Data-contract checks run after each indicator build (SPEC §9)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .catalog import Indicator


def _repo_file(path: str) -> Path | None:
    p = Path(path)
    if p.is_absolute():
        return p if p.exists() else None
    for parent in Path(__file__).resolve().parents:
        cand = parent / p
        if cand.exists():
            return cand
    cand = Path.cwd() / p
    return cand if cand.exists() else None


def run_checks(ind: Indicator, df: pd.DataFrame, ctx, previous: dict | None) -> list[dict]:
    results = []
    for spec in ind.qa:
        fn = CHECKS.get(spec["check"])
        try:
            res = fn(ind, df, ctx, previous, spec) if fn else {"status": "fail", "detail": "unknown check"}
        except Exception as e:  # a crashing check is a failed check
            res = {"status": "fail", "detail": f"error: {e}"}
        results.append({"check": spec["check"], **res})
    return results


def passed(results: list[dict]) -> bool:
    return all(r["status"] != "fail" for r in results)


def _land(ctx, df):
    is_water = ctx.universe["is_water"].to_numpy() if "is_water" in ctx.universe else np.zeros(len(df), bool)
    return ~is_water


def check_range(ind, df, ctx, prev, spec):
    v = df["raw"].to_numpy(float)
    v = v[~np.isnan(v)]
    bad = ((v < spec.get("min", -np.inf)) | (v > spec.get("max", np.inf))).sum()
    return {"status": "ok" if bad == 0 else "fail", "detail": f"{int(bad)} of {len(v)} values outside "
            f"[{spec.get('min')}, {spec.get('max')}]"}


def check_nodata_share(ind, df, ctx, prev, spec):
    land = _land(ctx, df)
    share = float(np.isnan(df["raw"].to_numpy(float)[land]).mean()) if land.any() else 0.0
    return {"status": "ok" if share <= spec.get("max", 1) else "fail", "detail": f"nodata share {share:.4f}"}


def check_nodata_share_settlement(ind, df, ctx, prev, spec):
    pop = ctx.cell_pop
    settled = pop > 0
    if not settled.any():
        return {"status": "skip", "detail": "no populated cells"}
    share = float(np.isnan(df["raw"].to_numpy(float)[settled]).mean())
    return {"status": "ok" if share <= spec.get("max", 0) else "fail",
            "detail": f"nodata share in settlement cells {share:.4f}"}


def check_count_delta(ind, df, ctx, prev, spec):
    valid = int((~np.isnan(df["raw"].to_numpy(float))).sum())
    if not prev or "stats" not in prev:
        return {"status": "skip", "detail": "no previous build"}
    before = prev["stats"].get("valid_cells") or 0
    if before == 0:
        return {"status": "skip", "detail": "previous build had no valid cells"}
    rel = abs(valid - before) / before
    return {"status": "ok" if rel <= spec.get("max_rel", 0.1) else "fail",
            "detail": f"valid cells {before} -> {valid} ({rel:.1%})"}


def check_reconcile_population(ind, df, ctx, prev, spec):
    if not ctx.has("fso_statpop"):
        return {"status": "skip", "detail": "STATPOP not staged"}
    from .staging import staged_path

    sp = pd.read_parquet(staged_path(ctx.store, ctx.state, "fso_statpop"))
    total, got = float(sp["pop"].sum()), float(ctx.cell_pop.sum())
    rel = abs(total - got) / total if total else 0
    return {"status": "ok" if rel <= spec.get("max_rel", 0.001) else "fail",
            "detail": f"universe population {got:,.0f} vs STATPOP {total:,.0f} ({rel:.3%})"}


def check_compare_reference(ind, df, ctx, prev, spec):
    p = _repo_file(f"catalog/reference/{spec['reference']}.json")
    if p is None:
        return {"status": "skip", "detail": f"reference {spec['reference']!r} not provided"}
    ref = json.loads(p.read_text())
    w = ctx.cell_pop if ctx.cell_pop.sum() > 0 else np.ones(len(df))
    v = df["raw"].to_numpy(float)
    ok = ~np.isnan(v)
    mean = float(np.average(v[ok], weights=w[ok])) if ok.any() and w[ok].sum() > 0 else float("nan")
    target = float(ref["value"])
    if "max_rel_diff" in spec:
        diff = abs(mean - target) / abs(target)
        good = diff <= spec["max_rel_diff"]
    else:
        diff = abs(mean - target)
        good = diff <= spec.get("max_abs_diff", 0)
    return {"status": "ok" if good else "fail", "detail": f"mean {mean:.4f} vs reference {target:.4f}"}


def check_golden_values(ind, df, ctx, prev, spec):
    p = _repo_file(spec["file"])
    if p is None:
        return {"status": "skip", "detail": f"golden file {spec['file']} not present"}
    golden = json.loads(p.read_text())
    tol = float(spec.get("tolerance", spec.get("tolerance_chf", 0)))
    raw_by_commune = getattr(ctx, "_commune_raw", {}).get(ind.id)
    misses = []
    for key, expected in golden.get("communes", {}).items():
        got = raw_by_commune.get(int(key)) if raw_by_commune else None
        if got is None or abs(got - float(expected)) > tol:
            misses.append(f"{key}: {got} != {expected}")
    for key, expected in golden.get("cells", {}).items():
        row = df.loc[df["cell_id"] == np.uint64(int(key, 16) if str(key).startswith("0x") else int(key))]
        got = float(row["raw"].iloc[0]) if len(row) else None
        if got is None or abs(got - float(expected)) > tol:
            misses.append(f"{key}: {got} != {expected}")
    return {"status": "ok" if not misses else "fail", "detail": "; ".join(misses[:10]) or "all match"}


def check_agreement(ind, df, ctx, prev, spec):
    cond = spec.get("applies_when", {})
    if any(ind.params.get(k) != v for k, v in cond.items()):
        return {"status": "skip", "detail": f"applies only when {cond}"}
    from .transforms.public_transport import are_classes

    ref = are_classes(ctx, ind.params | {"mode": "polygons"})
    pop = ctx.cell_pop > 0
    mask = pop if pop.any() else np.ones(len(df), bool)
    a = np.floor(df["raw"].to_numpy(float)[mask] / 2.5)
    b = np.floor(ref["raw"].to_numpy(float)[mask] / 2.5)
    share = float((a == b).mean()) if mask.any() else 1.0
    return {"status": "ok" if share >= spec.get("min", 0.97) else "fail",
            "detail": f"class agreement {share:.3f}"}


CHECKS = {
    "range": check_range,
    "nodata_share": check_nodata_share,
    "nodata_share_settlement": check_nodata_share_settlement,
    "count_delta_vs_previous": check_count_delta,
    "reconcile_population": check_reconcile_population,
    "compare_reference": check_compare_reference,
    "golden_values": check_golden_values,
    "agreement_with_reference": check_agreement,
}
