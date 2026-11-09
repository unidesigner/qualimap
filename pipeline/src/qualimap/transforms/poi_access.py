"""Access to education, dining, health and culture from OSM POIs (methodology §3.5).

Distance to the nearest POI of each subtype is the walking-network distance (multi-source
Dijkstra over the staged OSM walking graph). For the 2nd..k-th nearest POI the Euclidean
distance is scaled by the cell's observed network/Euclidean detour ratio. Without a graph, or
where a cell is more than ``max_snap_m`` from the network, Euclidean × ``detour_factor`` is
used and the cell is flagged.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from ..grid import to_lv95
from ..normalize import log_decay
from . import FLAG_EUCLIDEAN_FALLBACK

ALPHA = {1: [1.0], 2: [0.6, 0.4], 3: [0.5, 0.3, 0.2]}


@dataclass
class WalkGraph:
    matrix: object
    tree: cKDTree
    xy: np.ndarray


def _graph(ctx, sid: str) -> WalkGraph | None:
    cache = getattr(ctx, "_walk_graph", {})
    if sid in cache:
        return cache[sid]
    names = ctx.staged_names(sid) if ctx.has(sid) else []
    if "walk_edges.parquet" not in names:
        cache[sid] = None
        ctx._walk_graph = cache
        return None
    nodes = pd.read_parquet(ctx.staged(sid, "walk_nodes.parquet"))
    edges = pd.read_parquet(ctx.staged(sid, "walk_edges.parquet"))
    idx = pd.Series(np.arange(len(nodes)), index=nodes["id"].to_numpy())
    u = idx.reindex(edges["u"].to_numpy()).to_numpy()
    v = idx.reindex(edges["v"].to_numpy()).to_numpy()
    ok = ~(np.isnan(u) | np.isnan(v))
    n = len(nodes)
    m = coo_matrix((edges["length_m"].to_numpy(float)[ok], (u[ok].astype(int), v[ok].astype(int))), shape=(n, n)).tocsr()
    x, y = to_lv95(nodes["lon"].to_numpy(), nodes["lat"].to_numpy())
    xy = np.column_stack([x, y])
    g = WalkGraph(m, cKDTree(xy), xy)
    cache[sid] = g
    ctx._walk_graph = cache
    return g


def network_decay(ctx, params: dict) -> pd.DataFrame:
    sid = params.get("source", "osm_switzerland")
    max_snap = float(params.get("max_snap_m", 250))
    poi_snap = float(params.get("poi_snap_m", 200))
    detour = float(params.get("detour_factor", 1.35))
    pois = pd.read_parquet(ctx.staged(sid, "pois.parquet"))
    u = ctx.universe
    cx, cy = to_lv95(u["lon"].to_numpy(), u["lat"].to_numpy())
    cxy = np.column_stack([cx, cy])
    graph = _graph(ctx, sid) if params.get("network", "walk") == "walk" else None
    flags = np.zeros(len(u), dtype=np.uint32)
    if graph is not None:
        csnap, cnode = graph.tree.query(cxy)
        far = csnap > max_snap
    else:
        csnap, cnode, far = np.zeros(len(u)), np.zeros(len(u), int), np.ones(len(u), bool)
        ctx.note(f"{sid}: no walking graph staged; Euclidean × {detour} used")
    flags |= np.where(far, FLAG_EUCLIDEAN_FALLBACK, 0).astype(np.uint32)
    total = np.zeros(len(u))
    for st in params["subtypes"]:
        group, k = st["group"], int(st.get("k", 1))
        sel = pois[pois["groups"].apply(lambda gs, g=group: g in list(gs))]
        if sel.empty:
            ctx.note(f"{group}: no POIs; subtype scores 0")
            continue
        px, py = to_lv95(sel["lon"].to_numpy(), sel["lat"].to_numpy())
        pxy = np.column_stack([px, py])
        ptree = cKDTree(pxy)
        kk = min(k, len(sel))
        de, _ = ptree.query(cxy, k=kk)
        de = de.reshape(len(u), kk)
        d = de * detour
        if graph is not None:
            psnap, pnode = graph.tree.query(pxy)
            src = np.unique(pnode[psnap <= poi_snap])
            if len(src):
                dist = dijkstra(graph.matrix, directed=False, indices=src, min_only=True,
                                limit=float(st["dmax"]) * 1.5)
                dn = dist[cnode] + csnap
                good = ~far & np.isfinite(dn)
                ratio = np.where(good & (de[:, 0] > 1), np.clip(dn / np.maximum(de[:, 0], 1), 1.0, 3.0), detour)
                d[:, 0] = np.where(good, dn, d[:, 0])
                d[:, 0] = np.where(~good & ~far, float(st["dmax"]), d[:, 0])  # unreachable within limit
                for j in range(1, kk):
                    d[:, j] = np.maximum(de[:, j] * ratio, d[:, 0])
        alphas = ALPHA.get(k, [1.0 / k] * k)[:kk]
        alphas = np.array(alphas) / np.sum(alphas)
        s = sum(a * log_decay(d[:, j], float(st["d0"]), float(st["dmax"])) for j, a in enumerate(alphas))
        total += float(st["weight"]) * s
    return pd.DataFrame({"cell_id": ctx.cells, "raw": total, "flags": flags})
