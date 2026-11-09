"""Synthetic upstream data for an end-to-end pipeline test.

A ~4.5 km × 4.4 km test region near Zürich with two communes. Every catalog source is served
from a mocked HTTP endpoint (respx): the geo.admin.ch STAC API, plain HTTP downloads, the OSM
extract (as OSM XML) and the ESTV calculator.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import geopandas as gpd
import httpx
import numpy as np
import pandas as pd
import pytest
import rasterio
import respx
import shapely
from rasterio.transform import from_origin

from qualimap import grid

BBOX = (8.50, 47.36, 8.56, 47.40)
SPLIT_LON = 8.53
STAC = "https://data.geo.admin.ch/api/stac/v0.9"
ASSETS = "http://assets.test"
REPO = Path(__file__).resolve().parents[2]


def lv95_bbox():
    x, y = grid.to_lv95([BBOX[0], BBOX[2]], [BBOX[1], BBOX[3]])
    return float(x[0]), float(y[0]), float(x[1]), float(y[1])


def _gpkg(layers: dict[str, gpd.GeoDataFrame], path: Path) -> bytes:
    for name, gdf in layers.items():
        gdf.to_file(path, layer=name, driver="GPKG")
    return path.read_bytes()


def _tif(path: Path, fn, px=20.0) -> bytes:
    x0, y0, x1, y1 = lv95_bbox()
    x0, y0, x1, y1 = x0 - 500, y0 - 500, x1 + 500, y1 + 500
    w, h = int((x1 - x0) / px), int((y1 - y0) / px)
    X, Y = np.meshgrid(x0 + (np.arange(w) + 0.5) * px, y1 - (np.arange(h) + 0.5) * px)
    arr = fn(X, Y).astype(np.float32)
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=1, dtype="float32",
                       crs="EPSG:2056", transform=from_origin(x0, y1, px, px), nodata=-9999) as ds:
        ds.write(arr, 1)
    return path.read_bytes()


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, b in files.items():
            z.writestr(n, b)
    return buf.getvalue()


def _stac_items(collection: str, assets: dict[str, tuple[str, str]]) -> dict:
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature", "id": f"{collection}-item", "properties": {"updated": "2026-01-01T00:00:00Z"},
        "assets": {n: {"href": href, "type": mt, "file:checksum": f"1220{abs(hash(href)) % 10**12:012d}"}
                   for n, (href, mt) in assets.items()}}], "links": []}


def osm_xml() -> bytes:
    x0, y0, x1, y1 = BBOX
    nodes, ways = [], []
    nid = 1
    grid_ids = {}
    n = 12
    for i in range(n):
        for j in range(n):
            lon = x0 + (x1 - x0) * i / (n - 1)
            lat = y0 + (y1 - y0) * j / (n - 1)
            grid_ids[(i, j)] = nid
            nodes.append(f'<node id="{nid}" version="1" lat="{lat:.7f}" lon="{lon:.7f}"/>')
            nid += 1
    wid = 1
    for i in range(n):
        refs = "".join(f'<nd ref="{grid_ids[(i, j)]}"/>' for j in range(n))
        ways.append(f'<way id="{wid}" version="1">{refs}<tag k="highway" v="footway"/></way>')
        wid += 1
        refs = "".join(f'<nd ref="{grid_ids[(j, i)]}"/>' for j in range(n))
        ways.append(f'<way id="{wid}" version="1">{refs}<tag k="highway" v="residential"/></way>')
        wid += 1
    rng = np.random.default_rng(3)
    tags = ["amenity=school", "amenity=kindergarten", "amenity=restaurant", "amenity=cafe", "amenity=bar",
            "amenity=doctors", "amenity=pharmacy", "amenity=hospital", "amenity=dentist",
            "healthcare=physiotherapist", "tourism=museum", "amenity=theatre", "amenity=library",
            "amenity=university"]
    for k, tag in enumerate(tags * 3):
        lon, lat = rng.uniform(x0, x1), rng.uniform(y0, y1)
        key, val = tag.split("=")
        nodes.append(f'<node id="{nid}" version="1" lat="{lat:.7f}" lon="{lon:.7f}"><tag k="{key}" v="{val}"/>'
                     f'<tag k="name" v="POI {k}"/></node>')
        nid += 1
    # a private school that must be excluded
    nodes.append(f'<node id="{nid}" version="1" lat="{y0 + 0.01}" lon="{x0 + 0.01}"><tag k="amenity" v="school"/>'
                 f'<tag k="access" v="private"/></node>')
    body = "\n".join(nodes + ways)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6">\n{body}\n</osm>\n'.encode()


@pytest.fixture(scope="session")
def upstream(tmp_path_factory) -> dict:
    """Build every synthetic upstream file once per session."""
    d = tmp_path_factory.mktemp("upstream")
    x0, y0, x1, y1 = lv95_bbox()
    xs, _ = grid.to_lv95([SPLIT_LON, SPLIT_LON], [BBOX[1], BBOX[3]])
    xs = float(xs[0])
    land = shapely.box(x0, y0, x1, y1)
    communes = gpd.GeoDataFrame({"BFS_NUMMER": [261, 66], "NAME": ["Testikon", "Probstadt"],
                                 "KANTONSNUMMER": [1, 1], "OBJEKTART": ["Gemeindegebiet"] * 2},
                                geometry=[shapely.box(x0, y0, xs, y1), shapely.box(xs, y0, x1, y1)], crs="EPSG:2056")
    files: dict[str, bytes] = {}
    files["boundaries.gpkg"] = _gpkg({
        "TLM_LANDESGEBIET": gpd.GeoDataFrame({"ICC": ["CH"], "NAME": ["Schweiz"]}, geometry=[land], crs="EPSG:2056"),
        "TLM_HOHEITSGEBIET": communes,
    }, d / "boundaries.gpkg")
    files["water.gpkg"] = _gpkg({"TLM_STEHENDES_GEWAESSER": gpd.GeoDataFrame(
        {"NAME": ["Teich"]}, geometry=[shapely.box(x1 - 600, y0, x1, y0 + 400)], crs="EPSG:2056")}, d / "water.gpkg")
    rng = np.random.default_rng(42)
    nb = 400
    bx = np.concatenate([rng.normal((x0 + x1) / 2, 900, nb // 2), rng.uniform(x0, x1, nb // 2)]).clip(x0 + 5, x1 - 5)
    by = np.concatenate([rng.normal((y0 + y1) / 2, 900, nb // 2), rng.uniform(y0, y1, nb // 2)]).clip(y0 + 5, y1 - 5)
    gwr = pd.DataFrame({"EGID": np.arange(1, nb + 1), "GGDENR": np.where(bx < xs, 261, 66),
                        "GKODE": bx.round(1), "GKODN": by.round(1), "GSTAT": 1004,
                        "GKAT": rng.choice([1020, 1030, 1040, 1060], nb, p=[0.6, 0.15, 0.1, 0.15]),
                        "GANZWHG": rng.integers(1, 20, nb)})
    gwr.loc[:9, "GSTAT"] = 1007  # demolished: must be ignored
    files["gwr.csv"] = gwr.to_csv(sep="\t", index=False).encode()
    hx = (bx // 100).astype(int)
    hy = (by // 100).astype(int)
    hect = pd.DataFrame({"E_KOORD": hx * 100, "N_KOORD": hy * 100}).drop_duplicates()
    hect["BBTOT"] = rng.integers(3, 120, len(hect))
    orphan = pd.DataFrame({"E_KOORD": [int(x0 // 100 + 3) * 100], "N_KOORD": [int(y1 // 100 - 3) * 100], "BBTOT": [25]})
    files["statpop.csv"] = pd.concat([hect, orphan]).to_csv(sep=";", index=False).encode()
    files["statpop_total"] = str(int(pd.concat([hect, orphan])["BBTOT"].sum())).encode()
    gx = np.arange(int(x0 // 100) * 100, x1, 100)
    gy = np.arange(int(y0 // 100) * 100, y1, 100)
    GX, GY = np.meshgrid(gx, gy)
    dist = np.hypot(GX - (x0 + x1) / 2, GY - (y0 + y1) / 2)
    codes = np.where(dist < 1200, 11, np.where(dist < 1800, 21, np.where(dist < 2300, 41, 31)))
    files["nolc04.csv"] = pd.DataFrame({"E": GX.ravel(), "N": GY.ravel(), "LC": codes.ravel()}).to_csv(
        sep=";", index=False).encode()
    files["communes.csv"] = pd.DataFrame({"BFS": [261, 66], "NAME": ["Testikon", "Probstadt"]}).to_csv(index=False).encode()
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    road = lambda X, Y: 45 + 25 * np.exp(-np.abs(Y - cy) / 150)  # noqa: E731  an east-west road
    files["road_day.tif"] = _tif(d / "road_day.tif", road)
    files["road_night.tif"] = _tif(d / "road_night.tif", lambda X, Y: road(X, Y) - 8)
    files["rail_day.tif"] = _tif(d / "rail_day.tif", lambda X, Y: 40 + 20 * np.exp(-np.abs(X - cx) / 100))
    files["rail_night.tif"] = _tif(d / "rail_night.tif", lambda X, Y: 32 + 20 * np.exp(-np.abs(X - cx) / 100))
    files["no2.zip"] = _zip({"no2_2025.tif": _tif(d / "no2.tif", lambda X, Y: 15 + 20 * np.exp(-np.hypot(X - cx, Y - cy) / 1500))})
    files["pm10.zip"] = _zip({"pm10_2025.tif": _tif(d / "pm10.tif", lambda X, Y: 12 + 0 * X)})
    files["pm25.zip"] = _zip({"pm25_2025.tif": _tif(d / "pm25.tif", lambda X, Y: 8 + 3 * np.exp(-np.hypot(X - cx, Y - cy) / 1500))})
    files["foca.gpkg"] = _gpkg({"laerm": gpd.GeoDataFrame(
        {"level": [55.0, 60.0]}, geometry=[shapely.box(x0, y0, x0 + 1500, y0 + 1500), shapely.box(x0, y0, x0 + 700, y0 + 700)],
        crs="EPSG:2056")}, d / "foca.gpkg")
    centre = shapely.Point(cx, cy)
    are = gpd.GeoDataFrame({"klasse": ["A", "B", "C", "D"]},
                           geometry=[centre.buffer(r) for r in (300, 500, 750, 1000)], crs="EPSG:2056")
    files["are.gpkg"] = _gpkg({"oev": are}, d / "are.gpkg")
    roofs = gpd.GeoDataFrame({"KLASSE": rng.integers(1, 6, 50), "FLAECHE": rng.uniform(20, 200, 50),
                              "MSTRAHLUNG": rng.uniform(800, 1400, 50), "GWR_EGID": gwr["EGID"].iloc[10:60].to_numpy()},
                             geometry=[shapely.Point(x, y).buffer(5) for x, y in zip(bx[10:60], by[10:60], strict=False)],
                             crs="EPSG:2056")
    files["sonnendach.gpkg"] = _gpkg({"SOLKAT_CH_DACH": roofs}, d / "sonnendach.gpkg")
    fps = gpd.GeoDataFrame({"EGID": gwr["EGID"].to_numpy()},
                           geometry=[shapely.Point(x, y).buffer(9) for x, y in zip(bx, by, strict=False)], crs="EPSG:2056")
    files["buildings.gpkg"] = _gpkg({"footprints": fps}, d / "buildings.gpkg")
    files["osm.osm"] = osm_xml()
    files["gtfs.zip"] = gtfs_zip(cx, cy)
    return files


def gtfs_zip(cx: float, cy: float) -> bytes:
    lon, lat = grid.to_wgs84([cx, cx + 800], [cy, cy])
    stops = pd.DataFrame({"stop_id": ["S1", "S2"], "stop_name": ["Zentrum", "Ost"],
                          "stop_lat": lat, "stop_lon": lon, "parent_station": ["", ""]})
    routes = pd.DataFrame({"route_id": ["R_tram", "R_rail1", "R_rail2"], "route_type": ["0", "2", "2"]})
    trips, st = [], []
    for k in range(84):  # tram every 10 min each way 06:00–20:00
        for direction in (0, 1):
            tid = f"T{k}_{direction}"
            trips.append({"trip_id": tid, "route_id": "R_tram", "service_id": "WK", "direction_id": str(direction)})
            t = 6 * 60 + k * 10
            seq = [("S1", "S2"), ("S2", "S1")][direction]
            for i, sid in enumerate(seq):
                m = t + i * 3
                st.append({"trip_id": tid, "stop_id": sid, "stop_sequence": str(i + 1),
                           "arrival_time": f"{m // 60:02d}:{m % 60:02d}:00", "departure_time": f"{m // 60:02d}:{m % 60:02d}:00"})
    for k in range(28):  # two rail lines at S1, every 30 min each
        for r in ("R_rail1", "R_rail2"):
            tid = f"{r}_{k}"
            trips.append({"trip_id": tid, "route_id": r, "service_id": "WK", "direction_id": "0"})
            t = 6 * 60 + k * 30
            for i, sid in enumerate(["S1", "S2"]):
                m = t + i * 4
                st.append({"trip_id": tid, "stop_id": sid, "stop_sequence": str(i + 1),
                           "arrival_time": f"{m // 60:02d}:{m % 60:02d}:00", "departure_time": f"{m // 60:02d}:{m % 60:02d}:00"})
    cal = pd.DataFrame([{"service_id": "WK", "monday": "1", "tuesday": "1", "wednesday": "1", "thursday": "1",
                         "friday": "1", "saturday": "0", "sunday": "0", "start_date": "20260101", "end_date": "20271231"}])
    return _zip({"stops.txt": stops.to_csv(index=False).encode(), "routes.txt": routes.to_csv(index=False).encode(),
                 "trips.txt": pd.DataFrame(trips).to_csv(index=False).encode(),
                 "stop_times.txt": pd.DataFrame(st).to_csv(index=False).encode(),
                 "calendar.txt": cal.to_csv(index=False).encode()})


OVERLAY = """
meta:
  grid: {system: a5, base_resolution: 16, export_resolutions: [11, 12, 13, 14, 15, 16]}
sources:
  - id: sfoe_sonnendach
    provider: SFOE
    title: Sonnendach (test)
    kind: vector
    crs: EPSG:2056
    license: {id: OGD-CH-BY, attribution: "© BFE"}
    fetch: {method: http, url: "http://files.test/sonnendach.gpkg"}
    refresh: {every: P90D, check: etag}
    stage:
      layers:
        - layer: SOLKAT_CH_DACH
          columns: {klasse: KLASSE, flaeche: FLAECHE, mstrahlung: MSTRAHLUNG, gwr_egid: GWR_EGID}
          name: data.parquet
  - id: osm_switzerland
    provider: OSM
    title: OSM (test)
    kind: vector
    crs: EPSG:4326
    license: {id: ODbL-1.0, attribution: "© OpenStreetMap contributors"}
    fetch: {method: osm_pbf, url: "http://files.test/test.osm"}
    refresh: {every: P7D, check: etag}
  - id: fso_commune_register
    provider: FSO
    title: Commune register (test)
    kind: table
    license: {id: OGD-CH-BY, attribution: "© BFS"}
    fetch: {method: http, url: "${QM_COMMUNE_REGISTER_URL}", format: csv}
    refresh: {every: P30D, check: checksum}
"""


@pytest.fixture
def mocked(upstream):
    """respx router serving all synthetic upstream data."""
    f = upstream
    collections = {
        "ch.swisstopo.swissboundaries3d": {"gpkg": (f"{ASSETS}/boundaries.gpkg", "application/geopackage+sqlite3")},
        "ch.swisstopo.swissbuildings3d_3_0": {"gdb": (f"{ASSETS}/buildings.gpkg", "application/x.filegdb+zip")},
        "ch.swisstopo.swisstlm3d": {"gpkg": (f"{ASSETS}/water.gpkg", "application/geopackage+sqlite3")},
        "ch.are.gueteklassen_oev": {"gpkg": (f"{ASSETS}/are.gpkg", "application/geopackage+sqlite3")},
        "ch.bafu.laerm-strassenlaerm_tag": {"tif": (f"{ASSETS}/road_day.tif", "image/tiff")},
        "ch.bafu.laerm-strassenlaerm_nacht": {"tif": (f"{ASSETS}/road_night.tif", "image/tiff")},
        "ch.bafu.laerm-bahnlaerm_tag": {"tif": (f"{ASSETS}/rail_day.tif", "image/tiff")},
        "ch.bafu.laerm-bahnlaerm_nacht": {"tif": (f"{ASSETS}/rail_night.tif", "image/tiff")},
        "ch.bazl.laermbelastungskataster-zivilflugplaetze": {"gpkg": (f"{ASSETS}/foca.gpkg", "application/geopackage+sqlite3")},
        "ch.bafu.luftreinhaltung-stickstoffdioxid": {"zip": (f"{ASSETS}/no2.zip", "application/zip")},
        "ch.bafu.luftreinhaltung-feinstaub_pm10": {"zip": (f"{ASSETS}/pm10.zip", "application/zip")},
        "ch.bafu.luftreinhaltung-feinstaub_pm2_5": {"zip": (f"{ASSETS}/pm25.zip", "application/zip")},
    }
    calls = {"estv_calc": 0}
    with respx.mock(assert_all_called=False) as router:
        for col, assets in collections.items():
            router.get(f"{STAC}/collections/{col}/items").mock(
                return_value=httpx.Response(200, json=_stac_items(col, assets)))
        for name in ["boundaries.gpkg", "buildings.gpkg", "water.gpkg", "are.gpkg", "road_day.tif", "road_night.tif",
                     "rail_day.tif", "rail_night.tif", "foca.gpkg", "no2.zip", "pm10.zip", "pm25.zip"]:
            router.get(f"{ASSETS}/{name}").mock(return_value=httpx.Response(200, content=f[name]))
        plain = {"gwr.csv": "gwr.csv", "statpop.csv": "statpop.csv", "nolc04.csv": "nolc04.csv",
                 "communes.csv": "communes.csv", "sonnendach.gpkg": "sonnendach.gpkg", "test.osm": "osm.osm",
                 "gtfs.zip": "gtfs.zip"}
        for url_name, key in plain.items():
            router.get(f"http://files.test/{url_name}").mock(
                return_value=httpx.Response(200, content=f[key], headers={"etag": f'"{key}-v1"'}))
        estv = "https://swisstaxcalculator.estv.admin.ch/delegate/ost-integration/v1/lg-proxy/operation/c3b67379_ESTV"
        names = {"Testikon": 261, "Probstadt": 66}

        def search(request):
            q = json.loads(request.content)["Search"]
            return httpx.Response(200, json={"response": [{"TaxLocationID": 1000 + names[q], "BfsID": names[q], "City": q}]})

        def calc(request):
            calls["estv_calc"] += 1
            body = json.loads(request.content)
            total = 18000 if body["TaxLocationID"] == 1261 else 24000
            return httpx.Response(200, json={"response": {"TotalTax": total, "IncomeTaxCity": total / 2}})

        router.post(f"{estv}/API_searchLocation").mock(side_effect=search)
        router.post(f"{estv}/API_calculateDetailedTaxes").mock(side_effect=calc)
        router.calls_info = calls
        yield router


@pytest.fixture
def qm_env(tmp_path, monkeypatch, mocked):
    """A pipeline Env with local stores, the repo catalog plus the test overlay."""
    from qualimap.pipeline import Env
    from qualimap.settings import Settings

    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(OVERLAY)
    for k, v in {
        "QM_DATA_URL": (tmp_path / "data").as_uri(), "QM_PUB_URL": (tmp_path / "pub").as_uri(),
        "QM_CACHE_DIR": str(tmp_path / "cache"),
        "QM_GWR_URL": "http://files.test/gwr.csv", "QM_STATPOP_URL": "http://files.test/statpop.csv",
        "QM_NOLC04_URL": "http://files.test/nolc04.csv", "QM_COMMUNE_REGISTER_URL": "http://files.test/communes.csv",
        "QM_GTFS_URL": "http://files.test/gtfs.zip", "QM_STATPOP_POP_COLUMN": "BBTOT", "QM_NOLC04_CODE_COLUMN": "LC",
    }.items():
        monkeypatch.setenv(k, v)
    settings = Settings.from_env([str(REPO / "catalog" / "indicators.yaml"), str(overlay)])
    env = Env.create(settings)
    env.fetch_ctx_kwargs = {}
    return env
