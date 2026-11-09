"""End-to-end: fetch → stage → cells → compute → export → publish against mocked upstreams."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import zarr

from qualimap import pipeline as P
from qualimap import releases as R
from qualimap.compute import load_indicator


def test_full_run(qm_env, mocked, upstream):
    env = qm_env
    code, run = P.run(env, mode="due")
    steps = {s["step"]: s for s in run.steps}
    fetch_errors = [r for r in steps["fetch"]["results"] if r.get("error")]
    assert not fetch_errors, fetch_errors
    stage_errors = [r for r in steps["stage"]["results"] if r.get("error")]
    assert not stage_errors, stage_errors
    comp = {c["indicator"]: c for c in steps["compute"]["results"]}
    errors = {k: v["error"] for k, v in comp.items() if v["status"] == "error"}
    assert not errors, errors
    assert code == P.EXIT_OK, run.steps[-1]
    qa = {k: {c["check"]: c["status"] for c in v["qa"]} for k, v in comp.items()}
    assert all(v["qa_passed"] for v in comp.values()), qa
    assert qa["noise"] == {"range": "ok", "nodata_share_settlement": "ok"}
    assert qa["taxation"]["golden_values"] == "skip"  # no golden file in the repo yet
    assert run.release == "2026.11.1" or run.release.count(".") == 2

    # ---- release layout
    v = run.release
    latest = R.latest(env.pub)
    assert latest["version"] == v
    manifest = env.pub.get_json(f"releases/{v}/manifest.json")
    paths = {f["path"] for f in manifest["files"]}
    for r in range(11, 17):
        assert f"parquet/cells_r{r}.parquet" in paths
    assert "parquet/communes.parquet" in paths and "parquet/pois.parquet" in paths
    assert any(p.startswith("zarr/rasters/foen_sonbase_road_day.zarr/") for p in paths)
    assert manifest["sources"]["fso_gwr"]["sha256"]

    # ---- GeoParquet: one row group per res-11 parent, sorted ids, geo metadata
    pf = pq.ParquetFile(env.pub.local_path(f"releases/{v}/parquet/cells_r16.parquet"))
    t = pf.read().to_pandas()
    assert (np.diff(t["cell_id"].to_numpy(np.uint64).astype(np.float64)) > 0).all()
    geo = json.loads(pf.schema_arrow.metadata[b"geo"])
    assert geo["columns"]["geometry"]["encoding"] == "WKB"
    assert "covering" in geo["columns"]["geometry"]
    assert json.loads(pf.schema_arrow.metadata[b"qualimap"])["tile_resolution"] == 11
    stats = [pf.metadata.row_group(i).column(0).statistics for i in range(pf.num_row_groups)]
    for a, b in zip(stats, stats[1:], strict=False):
        assert a.max < b.min  # row groups do not overlap in cell_id
    for ind in ("noise", "air", "public_transport", "taxation", "education", "dining", "health", "culture", "green"):
        q = t[f"q_{ind}"].to_numpy()
        assert (q[q != 255] <= 250).all()
        assert (q != 255).mean() > 0.5, ind

    # ---- values make sense
    uni = pd.read_parquet(env.data.local_path("cells/universe_r16.parquet"))
    total = float(upstream["statpop_total"].decode())
    assert abs(uni["pop"].sum() - total) / total < 0.02  # buildings with GSTAT!=1004 dropped
    tax = load_indicator(env.data, env.state, "taxation").merge(uni[["cell_id", "commune_bfs"]], on="cell_id")
    by = tax.groupby("commune_bfs")["raw"].first()
    assert by[261] == 18000 and by[66] == 24000
    s261 = tax.loc[tax.commune_bfs == 261, "score"].iloc[0]
    s66 = tax.loc[tax.commune_bfs == 66, "score"].iloc[0]
    assert s261 > s66  # lower tax ranks better
    noise = load_indicator(env.data, env.state, "noise").merge(uni[["cell_id", "lat"]], on="cell_id")
    mid = noise.lat.between(noise.lat.quantile(0.45), noise.lat.quantile(0.55))
    assert noise.loc[mid, "raw"].mean() > noise.loc[~mid, "raw"].mean()  # the road is loud
    pt = load_indicator(env.data, env.state, "public_transport")
    assert pt["raw"].max() == 10 and set(np.unique(pt["raw"])) <= {0, 2.5, 5, 7.5, 10}

    # ---- zarr
    g = zarr.open_group(str(env.pub.local_path(f"releases/{v}/zarr/qualimap.zarr")), mode="r")
    assert g["r16"].attrs["dggs"]["name"] == "a5"
    pidx = g["r16/tile_index"][:]
    assert int(pidx[:, 1].sum()) == g["r16/cell_ids"].shape[0]

    # ---- catalog.json for the viewer
    cj = env.pub.get_json(f"releases/{v}/catalog.json")
    assert set(cj["present_indicators"]) >= {"noise", "taxation"}
    assert cj["files"]["cells"]["16"] == "parquet/cells_r16.parquet"

    # ---- second run: nothing changed upstream → nothing to do
    code2, run2 = P.run(env, mode="due")
    assert code2 == P.EXIT_NOTHING, run2.steps
    assert mocked.calls_info["estv_calc"] == 2  # not re-fetched: not due


def test_force_rebuild_and_diff(qm_env):
    env = qm_env
    code, run = P.run(env, mode="due")
    assert code == P.EXIT_OK
    code2, run2 = P.run(env, mode="all", force=True)
    assert code2 == P.EXIT_OK, run2.steps[-1]
    rel = [r["version"] for r in R.list_releases(env.pub)]
    assert len(rel) == 2
    d = R.diff(env.pub, rel[0], rel[1], res=13)
    assert (d["mean_abs_shift"].abs() < 1e-6).all()  # same inputs → identical scores
    R.switch_latest(env.pub, rel[0])
    assert R.latest(env.pub)["version"] == rel[0]


def test_maintenance_tools(qm_env):
    from qualimap.inspect_tools import inspect_cell
    from qualimap.maintenance import cors_rules, gc
    from qualimap.qa_tools import compare_neighbourhoods

    env = qm_env
    code, _ = P.run(env, mode="due")
    assert code == P.EXIT_OK
    rows = {r["method"]: r for r in compare_neighbourhoods(env, "green")}
    assert set(rows) == {"spherical_cap", "grid_disk", "grid_disk_vertex"}
    assert rows["spherical_cap"]["mean_abs_diff_vs_cap"] == 0
    info = inspect_cell(env, "8.53,47.38")
    assert info["resolution"] == 16 and info["indicators"]["noise"]["unit"] == "dB(A)"
    assert gc(env, keep_raw=3, dry_run=True)["removed"] == []
    assert cors_rules(["https://qualimap.ch"])["CORSRules"][0]["AllowedHeaders"][0] == "Range"
