"""Taxation: total tax of the reference household per commune (methodology §3.4).

Commune-level transform (``level: commune``); the framework ranks communes by population and
broadcasts the score to cells. Communes whose calculation failed get NaN (no data).
"""

from __future__ import annotations

import pandas as pd


def commune_total_tax(ctx, params: dict) -> pd.DataFrame:
    sid = params.get("source", "fta_tax_calculator")
    taxes = pd.read_parquet(ctx.staged(sid, "taxes.parquet"))
    taxes = taxes.dropna(subset=["total_tax"])
    out = taxes.groupby("commune_bfs", as_index=False)["total_tax"].mean()
    return out.rename(columns={"total_tax": "raw"})[["commune_bfs", "raw"]]
