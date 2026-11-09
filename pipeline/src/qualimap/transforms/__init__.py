"""Indicator transforms.

A transform is ``fn(ctx: Context, params: dict) -> pandas.DataFrame``. Cell-level transforms
return columns ``cell_id`` (uint64) and ``raw`` (float, NaN = no data); commune-level
transforms (``level: commune``) return ``commune_bfs`` and ``raw``. An optional ``flags``
column (uint32 bit field) is carried to the outputs.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from importlib.metadata import entry_points

import numpy as np

FLAG_STALE_SOURCE = 1 << 0
FLAG_LIMIT_EXCEEDED = 1 << 1
FLAG_EUCLIDEAN_FALLBACK = 1 << 2
FLAG_PARENT_FILLED = 1 << 3
FLAGS = {
    "stale_source": FLAG_STALE_SOURCE,
    "limit_exceeded": FLAG_LIMIT_EXCEEDED,
    "euclidean_fallback": FLAG_EUCLIDEAN_FALLBACK,
    "parent_filled": FLAG_PARENT_FILLED,
}


def load_transform(path: str) -> Callable:
    for ep in entry_points(group="qualimap.transforms"):
        if ep.name == path:
            return ep.load()
    module, _, name = path.partition(":")
    return getattr(importlib.import_module(module), name)


def energetic_sum(levels: list[np.ndarray]) -> np.ndarray:
    """10·log10(Σ 10^(L/10)) ignoring NaN; NaN where every source is NaN."""
    stack = np.vstack([np.asarray(lv, float) for lv in levels])
    energy = np.where(np.isnan(stack), 0.0, 10 ** (stack / 10.0)).sum(axis=0)
    with np.errstate(divide="ignore"):
        out = 10 * np.log10(energy)
    return np.where(np.isnan(stack).all(axis=0), np.nan, out)
