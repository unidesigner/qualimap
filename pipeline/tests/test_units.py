"""Unit tests for catalog, normalisation, grid, transforms, fetchers and CLI."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import a5
import httpx
import numpy as np
import pandas as pd
import pytest
import respx
from shapely.geometry import box
from typer.testing import CliRunner

from qualimap import grid
from qualimap import normalize as N
from qualimap.catalog import Catalog, interpolate, merge, parse_duration
from qualimap.transforms import energetic_sum
from qualimap.transforms.public_transport import (
    are_classes,
    category_for,
    class_at,
    continuous_score,
    stop_categories,
)

REPO = Path(__file__).resolve().parents[2]
CATALOG = REPO / "catalog" / "indicators.yaml"


# ------------------------------------------------------------------ catalog
def test_repo_catalog_is_valid():
    cat = Catalog.load([CATALOG])
    assert cat.validate() == []
    assert len(cat.dimensions) == 9
    assert {i.dimension for i in cat.indicators.values() if i.scored} == set(cat.dimensions)


def test_catalog_errors_are_reported(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("""
indicators:
  - id: noise
    dimension: noise
    title: {en: x}
    inputs: [does_not_exist]
    transform: {fn: qualimap.transforms.noise:effective_level, version: 1}
    unit: dB
    aggregation: pop_weighted_mean
    normalize: {method: linear, good: 45, bad: 65}
    qa: [{check: no_such_check}]
""")
    errs = Catalog.load([CATALOG, bad]).validate()
    assert any("unknown input 'does_not_exist'" in e for e in errs)
    assert any("unknown qa check" in e for e in errs)


def test_layering_and_interpolation():
    base = {"sources": [{"id": "a", "x": 1}, {"id": "b", "x": 2}], "meta": {"grid": {"base_resolution": 16}}}
    layer = {"sources": [{"id": "b", "x": 3}, {"id": "c", "x": 4}], "meta": {"grid": {"base_resolution": 15}}}
    m = merge(base, layer)
    assert [s["id"] for s in m["sources"]] == ["a", "b", "c"]
    assert m["sources"][1]["x"] == 3 and m["meta"]["grid"]["base_resolution"] == 15
    assert interpolate({"u": "${A}/x", "v": ["${B}"]}, {"A": "http://h"}) == {"u": "http://h/x", "v": ["${B}"]}
    assert parse_duration("P7D").days == 7 and parse_duration("P1Y").days == 365


def test_graph_formats():
    cat = Catalog.load([CATALOG])
    assert "source:foen_no2" in cat.graph("mermaid")
    assert cat.graph("dot").startswith("digraph")
    assert set(cat.downstream({"osm_switzerland"})) == {"education", "dining", "health", "culture"}


# ------------------------------------------------------------------ normalisation
def test_linear_both_directions():
    np.testing.assert_allclose(N.linear(np.array([45, 55, 65, 70]), good=45, bad=65), [10, 5, 0, 0])
    np.testing.assert_allclose(N.linear(np.array([0.1, 0.35, 0.6]), good=0.6, bad=0.1), [0, 5, 10])


def test_log_decay_midpoint():
    s = N.log_decay(np.array([100, np.sqrt(200 * 2000), 2000, 9999]), 200, 2000)
    np.testing.assert_allclose(s, [10, 5, 0, 0], atol=1e-9)


def test_percentile_weighted_midrank():
    x = np.array([10.0, 20.0, 20.0, 30.0, np.nan])
    w = np.array([1.0, 1.0, 1.0, 1.0, 5.0])
    pr = N.percentile_rank(x, w)
    np.testing.assert_allclose(pr[:4], [0.125, 0.5, 0.5, 0.875])
    assert np.isnan(pr[4])
    s = N.percentile(x, w, "lower_is_better")
    assert s[0] > s[3]


def test_quantize_roundtrip():
    q = N.quantize(np.array([0.0, 5.0, 10.0, np.nan]))
    assert q.tolist() == [0, 125, 250, 255]
    np.testing.assert_allclose(N.dequantize(q)[:3], [0, 5, 10])


# ------------------------------------------------------------------ grid
def test_descendants_are_contiguous_and_sorted():
    c = a5.lonlat_to_cell((8.54, 47.37), 11)
    kids = np.array(a5.cell_to_children(c, 16), dtype=np.uint64)
    assert len(kids) == 1024
    assert (np.diff(kids.astype(np.float64)) > 0).all()
    lo, hi = grid.descendant_range(c, 16)
    assert lo == int(kids[0]) and hi == int(kids[-1])


def test_universe_shares_and_parents():
    u = grid.build_universe(box(8.50, 47.36, 8.53, 47.38), 15)
    assert u["cell_id"].is_monotonic_increasing
    assert (u["in_share"] > 0).all() and (u["in_share"] <= 1).all()
    assert (u["in_share"] < 1).any()  # border cells exist
    p = grid.parents(u["cell_id"].to_numpy(np.uint64), 11)
    assert (p == u["parent_r11"].to_numpy(np.uint64)).all()


def test_derive_k_matches_spec_table():
    assert grid.derive_k(16, 300) == 3
    st = grid.disk_stats(16, 3)
    assert 30 <= st["cells"] <= 36
    assert 270 < st["equivalent_radius_m"] < 300


def test_neighbourhood_methods_agree_roughly():
    u = grid.build_universe(box(8.50, 47.36, 8.52, 47.375), 16)
    cells = u["cell_id"].to_numpy(np.uint64)
    ones = np.ones(len(cells))
    kw = {"lon": u["lon"].to_numpy(), "lat": u["lat"].to_numpy(), "res": 16}
    _, n_cap = grid.neighbourhood_sum(cells, ones, "spherical_cap", 300, **kw)
    _, n_disk = grid.neighbourhood_sum(cells, ones, "grid_disk", 300, **kw)
    _, n_vert = grid.neighbourhood_sum(cells, ones, "grid_disk_vertex", 300, k=2, **kw)
    interior = n_cap == n_cap.max()
    assert 33 <= np.median(n_cap[interior]) <= 40
    assert np.median(n_disk[interior]) <= 33 and np.median(n_vert[interior]) <= 23


def test_energetic_rollup():
    df = pd.DataFrame({"p": np.array([1, 1], dtype=np.uint64), "L": [60.0, 60.0]})
    r = grid.weighted_rollup(df, "p", ["L"], None, energetic=["L"])
    assert abs(r["L"].iloc[0] - 60.0) < 1e-9
    assert abs(energetic_sum([np.array([60.0]), np.array([60.0])])[0] - 63.0103) < 1e-3


# ------------------------------------------------------------------ public transport
def test_are_tables():
    assert category_for(4, "rail_node") == "I" and category_for(30, "road") == "V"
    assert category_for(90, "road") is None
    assert class_at("I", 299) == "A" and class_at("I", 600) == "B" and class_at("V", 400) is None
    scores = {"A": 10, "B": 7.5, "C": 5, "D": 2.5, "none": 0}
    assert continuous_score("I", 0, scores) == 10
    assert continuous_score("I", 400, scores) == pytest.approx(8.75)
    assert continuous_score("II", 1000, scores) == 0


def test_stop_categories_from_gtfs(upstream, tmp_path):
    import io
    import zipfile

    z = zipfile.ZipFile(io.BytesIO(upstream["gtfs.zip"]))
    tables = {n[:-4]: pd.read_csv(z.open(n), dtype=str, keep_default_na=False) for n in z.namelist()}
    cats = stop_categories(tables, date(2026, 11, 10))
    by = cats.set_index("stop_key")
    # S1 (departures only in one direction, S1 is the terminus of the return trips):
    # tram 84 departures → 10 min → road category IV; two rail lines 56 departures → 15 min,
    # rail node → II. The best category wins.
    assert by.loc["S1", "category"] == "II"
    assert by.loc["S1", "interval_min"] == pytest.approx(10, abs=0.01)


# ------------------------------------------------------------------ fetchers
def test_http_etag_304(tmp_path):
    from qualimap.fetchers import FetchContext, get_fetcher
    from qualimap.state import StateStore
    from qualimap.store import Store

    cat = Catalog({"sources": [{"id": "s", "provider": "x", "title": "t", "kind": "table",
                                "license": {"id": "x", "attribution": "x"},
                                "fetch": {"method": "http", "url": "http://f.test/a.csv"},
                                "refresh": {"every": "P1D", "check": "etag"}}]})
    store = Store((tmp_path / "d").as_uri())
    ctx = FetchContext(cat, store, StateStore(store), workdir=tmp_path / "w", backoff_s=0)
    with respx.mock() as r:
        route = r.get("http://f.test/a.csv")
        route.side_effect = [httpx.Response(200, content=b"a,b\n1,2\n", headers={"etag": '"v1"'}),
                             httpx.Response(304)]
        f = get_fetcher("http")(cat.source("s"), ctx)
        first = f.fetch()
        second = f.fetch()
        assert first.changed and not second.changed
        assert route.calls[1].request.headers["If-None-Match"] == '"v1"'


def test_stac_unchanged_checksums(tmp_path):
    from qualimap.fetchers import FetchContext, get_fetcher
    from qualimap.state import StateStore
    from qualimap.store import Store

    cat = Catalog({"sources": [{"id": "s", "provider": "x", "title": "t", "kind": "raster",
                                "license": {"id": "x", "attribution": "x"},
                                "fetch": {"method": "stac", "collection": "c", "asset_media_type": "image/tiff"},
                                "refresh": {"every": "P1D", "check": "checksum"}}]})
    store = Store((tmp_path / "d").as_uri())
    ctx = FetchContext(cat, store, StateStore(store), workdir=tmp_path / "w", backoff_s=0)
    items = {"features": [{"id": "i", "properties": {"updated": "x"}, "assets": {
        "t": {"href": "http://a.test/x.tif", "type": "image/tiff; application=geotiff", "file:checksum": "1220ab"},
        "j": {"href": "http://a.test/x.json", "type": "application/json"}}}], "links": []}
    with respx.mock() as r:
        r.get("https://data.geo.admin.ch/api/stac/v0.9/collections/c/items").mock(return_value=httpx.Response(200, json=items))
        dl = r.get("http://a.test/x.tif").mock(return_value=httpx.Response(200, content=b"TIFF"))
        f = get_fetcher("stac")(cat.source("s"), ctx)
        assert f.fetch().changed
        assert not f.fetch().changed
        assert dl.call_count == 1  # second run skipped the download entirely


# ------------------------------------------------------------------ CLI
def test_cli_offline(tmp_path, monkeypatch):
    from qualimap.cli import app

    monkeypatch.setenv("QM_DATA_URL", (tmp_path / "d").as_uri())
    monkeypatch.setenv("QM_PUB_URL", (tmp_path / "p").as_uri())
    monkeypatch.setenv("QM_CACHE_DIR", str(tmp_path / "c"))
    runner = CliRunner()
    r = runner.invoke(app, ["catalog", "validate"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["--json", "sources", "list"])
    rows = json.loads(r.output)
    assert {row["id"] for row in rows} >= {"fso_gwr", "osm_switzerland"}
    r = runner.invoke(app, ["--json", "demo", "--bbox", "8.50,47.36,8.53,47.38", "--res", "15"])
    assert r.exit_code == 0, r.output
    v = json.loads(r.output)["version"]
    r = runner.invoke(app, ["--json", "releases", "list"])
    assert json.loads(r.output)[0]["version"] == v
    r = runner.invoke(app, ["--json", "inspect", "commune", "9000"])
    assert r.exit_code == 0, r.output
    assert "noise" in json.loads(r.output)["indicators"]
    r = runner.invoke(app, ["--json", "--dry-run", "gc"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["catalog", "graph", "--format", "dot"])
    assert "digraph" in r.output


def test_are_polygon_mode(tmp_path):
    """are_classes in polygon mode picks the best class among overlapping rings."""
    import geopandas as gpd
    import shapely

    from qualimap.context import Context
    from qualimap.state import StateStore
    from qualimap.store import Store

    store = Store((tmp_path / "d").as_uri())
    state = StateStore(store)
    u = grid.build_universe(box(8.50, 47.36, 8.52, 47.375), 15)
    u["pop"] = 1.0
    u.to_parquet(tmp_path / "u.parquet")
    store.put_file(tmp_path / "u.parquet", "cells/universe_r15.parquet")
    c = shapely.Point(8.51, 47.3675)
    rings = gpd.GeoDataFrame({"klasse": ["A", "C"]}, geometry=[c.buffer(0.002), c.buffer(0.006)], crs="EPSG:4326")
    rings.to_parquet(tmp_path / "are.parquet")
    store.put_file(tmp_path / "are.parquet", "staged/are/x/data.parquet")
    state.save_staged_record("are", {"raw_sha256": "x", "files": {"data.parquet": "staged/are/x/data.parquet"}})
    ctx = Context(Catalog({}), store, state, 15)
    out = are_classes(ctx, {"mode": "polygons", "source": "are", "class_field": "klasse"})
    assert set(out["raw"].unique()) <= {0.0, 5.0, 10.0}
    assert (out["raw"] == 10).any() and (out["raw"] == 5).any()
