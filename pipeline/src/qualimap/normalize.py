"""Normalisation of raw indicator values to scores in [0, 10] and uint8 quantisation (SPEC §1.4 of
the methodology; catalog ``normalize`` block)."""

from __future__ import annotations

import numpy as np

NODATA = 255
SCALE = 25.0


def linear(x: np.ndarray, good: float, bad: float) -> np.ndarray:
    """10 at ``good`` (or beyond), 0 at ``bad`` (or beyond); works in both directions."""
    x = np.asarray(x, float)
    if good == bad:
        raise ValueError("linear normalisation needs good != bad")
    return 10.0 * np.clip((x - bad) / (good - bad), 0.0, 1.0)


def log_decay(d: np.ndarray, d0: float, dmax: float) -> np.ndarray:
    """10 for d <= d0, 0 for d >= dmax, logarithmic in between (5 at sqrt(d0*dmax))."""
    d = np.asarray(d, float)
    if not (0 < d0 < dmax):
        raise ValueError("log_decay needs 0 < d0 < dmax")
    with np.errstate(divide="ignore", invalid="ignore"):
        s = 10.0 * (1.0 - np.log(np.maximum(d, d0) / d0) / np.log(dmax / d0))
    s = np.clip(s, 0.0, 10.0)
    return np.where(np.isnan(d), np.nan, s)


def percentile_rank(x: np.ndarray, weights: np.ndarray | None = None) -> np.ndarray:
    """Weighted mid-rank in [0, 1]: (weight below + half the weight of ties) / total weight."""
    x = np.asarray(x, float)
    w = np.ones_like(x) if weights is None else np.asarray(weights, float)
    ok = ~np.isnan(x)
    out = np.full(len(x), np.nan)
    if not ok.any():
        return out
    xv, wv = x[ok], w[ok]
    total = wv.sum()
    if total <= 0:
        wv = np.ones_like(xv)
        total = wv.sum()
    order = np.argsort(xv, kind="stable")
    xs, ws = xv[order], wv[order]
    uniq, start = np.unique(xs, return_index=True)
    tie_w = np.add.reduceat(ws, start)
    below = np.concatenate([[0.0], np.cumsum(tie_w)[:-1]])
    pr_u = (below + 0.5 * tie_w) / total
    out[ok] = pr_u[np.searchsorted(uniq, xv)]
    return out


def percentile(x: np.ndarray, weights: np.ndarray | None, direction: str) -> np.ndarray:
    pr = percentile_rank(x, weights)
    if direction == "lower_is_better":
        pr = 1.0 - pr
    elif direction != "higher_is_better":
        raise ValueError(f"unknown direction {direction!r}")
    return 10.0 * pr


def apply(raw: np.ndarray, spec: dict, weights: np.ndarray | None = None) -> np.ndarray:
    """Score in [0, 10] (NaN where raw is NaN) for a catalog ``normalize`` block."""
    method = spec["method"]
    raw = np.asarray(raw, float)
    if method == "linear":
        s = linear(raw, float(spec["good"]), float(spec["bad"]))
    elif method == "percentile":
        s = percentile(raw, weights if spec.get("weight", "population") == "population" else None,
                       spec.get("direction", "higher_is_better"))
    elif method in ("lookup", "log_decay"):
        # The transform already produced a score in [0, 10] (class lookup, or decay applied
        # per POI subtype with subtype-specific d0/dmax).
        s = np.clip(raw, 0.0, 10.0)
    else:
        raise ValueError(f"unknown normalisation method {method!r}")
    return np.where(np.isnan(raw), np.nan, s)


def quantize(score: np.ndarray, scale: float = SCALE, nodata: int = NODATA) -> np.ndarray:
    s = np.asarray(score, float)
    q = np.round(np.clip(s, 0, 10) * scale)
    q = np.where(np.isnan(s), nodata, q)
    return q.astype(np.uint8)


def dequantize(q: np.ndarray, scale: float = SCALE, nodata: int = NODATA) -> np.ndarray:
    q = np.asarray(q)
    return np.where(q == nodata, np.nan, q / scale)
