"""Stage 2: normalise raw downloads into Parquet / GeoParquet / COG (SPEC §2).

Staged files live at ``staged/<source_id>/<sha12>/<name>``; a record in
``state/staged/<source_id>.json`` maps logical names to keys, so later stages ask for
``staged_path(ctx, "fso_gwr", "data.parquet")`` without knowing raw filenames.
"""

from __future__ import annotations

import json
import math
import shutil
import tempfile
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio

from .catalog import Catalog, Source
from .state import StateStore
from .store import Store

WALK_HIGHWAYS = {
    "footway", "path", "pedestrian", "living_street", "residential", "service", "unclassified",
    "tertiary", "tertiary_link", "secondary", "secondary_link", "primary", "primary_link",
    "steps", "track", "cycleway", "bridleway", "corridor",
}
NO_WALK_HIGHWAYS = {"motorway", "motorway_link", "trunk", "trunk_link"}
LIFECYCLE = ("disused:", "abandoned:", "demolished:", "construction:", "proposed:")


class StagingError(RuntimeError):
    pass


# ---------------------------------------------------------------------------- public API
def stage_source(catalog: Catalog, store: Store, state: StateStore, sid: str, force: bool = False) -> dict:
    src = catalog.source(sid)
    rec = state.fetch_record(sid)
    if src.method == "none":
        return {"source_id": sid, "skipped": "nothing to stage"}
    if rec is None:
        raise StagingError(f"{sid}: no fetch record; run `qm fetch {sid}` first")
    prev = state.staged_record(sid)
    if prev and prev.get("raw_sha256") == rec.sha256 and not force:
        return prev | {"unchanged": True}
    sha12 = rec.sha256[:12]
    with tempfile.TemporaryDirectory(prefix=f"qm-stage-{sid}-") as tmp:
        tmp = Path(tmp)
        raws = [store.local_path(f["key"]) for f in rec.files]
        out = tmp / "out"
        out.mkdir()
        produced = STAGERS[_stager_for(src)](src, raws, out, tmp, catalog)
        files = {}
        for name in produced:
            key = f"staged/{sid}/{sha12}/{name}"
            store.put_file(out / name, key)
            files[name] = key
    record = {"source_id": sid, "raw_sha256": rec.sha256, "files": files}
    state.save_staged_record(sid, record)
    return record


def staged_key(state: StateStore, sid: str, name: str | None = None) -> str:
    rec = state.staged_record(sid)
    if not rec or not rec.get("files"):
        raise StagingError(f"{sid}: not staged yet; run `qm stage {sid}`")
    files = rec["files"]
    if name is None:
        if len(files) != 1:
            raise StagingError(f"{sid}: several staged files {sorted(files)}; name one")
        return next(iter(files.values()))
    if name not in files:
        raise StagingError(f"{sid}: no staged file {name!r} (have {sorted(files)})")
    return files[name]


def staged_path(store: Store, state: StateStore, sid: str, name: str | None = None) -> Path:
    return store.local_path(staged_key(state, sid, name))


def staged_names(state: StateStore, sid: str) -> list[str]:
    rec = state.staged_record(sid) or {}
    return sorted(rec.get("files", {}))


def load_communes(catalog: Catalog, store: Store, state: StateStore) -> gpd.GeoDataFrame:
    region = catalog.meta.get("region", {})
    sid = region.get("boundary_source")
    gdf = gpd.read_parquet(staged_path(store, state, sid, region.get("commune_file", "communes.parquet")))
    return gdf


def load_boundary(catalog: Catalog, store: Store, state: StateStore):
    region = catalog.meta.get("region", {})
    sid = region.get("boundary_source")
    gdf = gpd.read_parquet(staged_path(store, state, sid, region.get("boundary_file", "boundary.parquet")))
    return gdf.to_crs("EPSG:4326").union_all()


# ---------------------------------------------------------------------------- dispatch
def _stager_for(src: Source) -> str:
    if src.method == "osm_pbf":
        return "osm_pbf"
    if src.method == "gtfs":
        return "gtfs"
    if src.method == "estv_api":
        return "estv"
    return src.kind


def _extract(raws: list[Path], tmp: Path) -> list[Path]:
    files: list[Path] = []
    for r in raws:
        if zipfile.is_zipfile(r) and not r.name.endswith(".gdb.zip"):
            d = tmp / f"x_{r.stem}"
            with zipfile.ZipFile(r) as z:
                z.extractall(d)
            files += [p for p in d.rglob("*")]
        else:
            files.append(r)
    return files


def _vector_paths(raws: list[Path], tmp: Path) -> list[str]:
    paths: list[str] = []
    for r in raws:
        if r.name.endswith(".gdb.zip") or (zipfile.is_zipfile(r) and _zip_has_gdb(r)):
            inner = _zip_gdb_name(r)
            paths.append(f"/vsizip/{r}/{inner}" if inner else f"/vsizip/{r}")
        elif zipfile.is_zipfile(r):
            for p in _extract([r], tmp):
                if p.suffix.lower() in (".gpkg", ".shp", ".geojson", ".json", ".fgb", ".parquet"):
                    paths.append(str(p))
                elif p.is_dir() and p.suffix.lower() == ".gdb":
                    paths.append(str(p))
        else:
            paths.append(str(r))
    if not paths:
        raise StagingError(f"no vector dataset in {[r.name for r in raws]}")
    return paths


def _zip_has_gdb(r: Path) -> bool:
    with zipfile.ZipFile(r) as z:
        return any(".gdb/" in n for n in z.namelist())


def _zip_gdb_name(r: Path) -> str | None:
    with zipfile.ZipFile(r) as z:
        for n in z.namelist():
            if ".gdb/" in n:
                return n.split(".gdb/")[0] + ".gdb"
    return None


def _read_vector(path: str, layer: str | None) -> gpd.GeoDataFrame:
    if path.endswith(".parquet"):
        return gpd.read_parquet(path)
    return gpd.read_file(path, layer=layer, engine="pyogrio")


def _apply(gdf: pd.DataFrame, spec: dict) -> pd.DataFrame:
    if spec.get("where"):
        gdf = gdf.query(spec["where"])
    if spec.get("columns"):
        mapping = {src: target for target, src in spec["columns"].items()}  # catalog: target: source
        missing = [src for src in mapping if src not in gdf.columns]
        if missing:
            raise StagingError(f"columns {missing} not found (have {list(gdf.columns)[:30]})")
        gdf = gdf.rename(columns=mapping)
        keep = list(spec["columns"]) + (["geometry"] if "geometry" in gdf.columns else [])
        gdf = gdf[keep]
    return gdf


# ---------------------------------------------------------------------------- stagers
def stage_vector(src: Source, raws, out: Path, tmp: Path, catalog: Catalog) -> list[str]:
    paths = _vector_paths(raws, tmp)
    layers = src.stage.get("layers") or [{"name": "data.parquet"}]
    produced = []
    for spec in layers:
        frames = []
        for p in paths:
            available = [lyr[0] for lyr in pyogrio.list_layers(p)] if not p.endswith(".parquet") else [None]
            layer = spec.get("layer")
            if layer and layer not in available:
                continue
            frames.append(_read_vector(p, layer or available[0]))
        if not frames:
            raise StagingError(f"{src.id}: layer {spec.get('layer')!r} not found in {paths}")
        gdf = pd.concat(frames, ignore_index=True)
        gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=frames[0].crs)
        if gdf.crs is None:
            gdf = gdf.set_crs(src.raw.get("crs", "EPSG:4326"))
        gdf = gpd.GeoDataFrame(_apply(gdf, spec), geometry="geometry", crs=gdf.crs).to_crs("EPSG:4326")
        if spec.get("dissolve"):
            gdf = gdf.dissolve(by=spec["dissolve"], as_index=False)
        name = spec.get("name", "data.parquet")
        gdf.to_parquet(out / name, index=False)
        produced.append(name)
    return produced


def stage_raster(src: Source, raws, out: Path, tmp: Path, catalog: Catalog) -> list[str]:
    import fnmatch

    import rasterio
    from rasterio.shutil import copy as rio_copy

    pattern = src.stage.get("select", "*")
    cands = [p for p in _extract(raws, tmp)
             if p.is_file() and p.suffix.lower() in (".tif", ".tiff", ".asc", ".img")
             and fnmatch.fnmatch(p.name, pattern)]
    if not cands:
        raise StagingError(f"{src.id}: no raster matching {pattern!r}")
    cands.sort(key=lambda p: p.stat().st_size, reverse=True)
    names = src.stage.get("names") or (["data.tif"] if len(cands) == 1 or not src.stage.get("all") else
                                        [f"{p.stem}.tif" for p in cands])
    produced = []
    for p, name in zip(cands, names, strict=False):
        with rasterio.open(p) as ds:
            if ds.crs is None and src.raw.get("crs"):
                tmp_tif = tmp / f"crs_{name}"
                profile = ds.profile | {"crs": src.raw["crs"], "driver": "GTiff"}
                with rasterio.open(tmp_tif, "w", **profile) as dst:
                    dst.write(ds.read())
                p = tmp_tif
        rio_copy(p, out / name, driver="COG", compress="DEFLATE")
        produced.append(name)
    return produced


def stage_table(src: Source, raws, out: Path, tmp: Path, catalog: Catalog) -> list[str]:
    spec = src.stage
    files = [p for p in _extract(raws, tmp) if p.is_file()]
    pattern = spec.get("select")
    if pattern:
        import fnmatch

        files = [p for p in files if fnmatch.fnmatch(p.name, pattern)]
    fmt = src.fetch.get("format", files[0].suffix.lstrip(".") if files else "csv")
    frames = []
    for p in files:
        if fmt in ("csv", "txt", "tsv") or p.suffix.lower() in (".csv", ".txt", ".tsv"):
            frames.append(pd.read_csv(p, sep=spec.get("sep") or None, engine="python",
                                      encoding=spec.get("encoding", "utf-8"), dtype=spec.get("dtype")))
        elif fmt == "xml" or p.suffix.lower() == ".xml":
            frames.append(pd.read_xml(p, xpath=spec.get("xpath", ".//*")))
        elif p.suffix.lower() == ".parquet":
            frames.append(pd.read_parquet(p))
    if not frames:
        raise StagingError(f"{src.id}: no table files found")
    df = _apply(pd.concat(frames, ignore_index=True), spec)
    name = spec.get("name", "data.parquet")
    df.to_parquet(out / name, index=False)
    return [name]


def stage_estv(src: Source, raws, out: Path, tmp: Path, catalog: Catalog) -> list[str]:
    rows = []
    for r in raws:
        for line in Path(r).read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                rows.append({k: rec.get(k) for k in ("commune_bfs", "name", "location_id", "total_tax", "error")})
    pd.DataFrame(rows).to_parquet(out / "taxes.parquet", index=False)
    return ["taxes.parquet"]


def stage_gtfs(src: Source, raws, out: Path, tmp: Path, catalog: Catalog) -> list[str]:
    produced = []
    with zipfile.ZipFile(raws[0]) as z:
        for table in ("stops", "routes", "trips", "stop_times", "calendar", "calendar_dates"):
            name = f"{table}.txt"
            if name not in z.namelist():
                continue
            with z.open(name) as fh:
                df = pd.read_csv(fh, dtype=str, keep_default_na=False)
            df.to_parquet(out / f"{table}.parquet", index=False)
            produced.append(f"{table}.parquet")
    if "stops.parquet" not in produced:
        raise StagingError(f"{src.id}: not a GTFS feed (stops.txt missing)")
    return produced


def stage_osm(src: Source, raws, out: Path, tmp: Path, catalog: Catalog) -> list[str]:
    try:
        import osmium
    except ImportError as e:  # pragma: no cover
        raise StagingError("install qualimap[osm] (pyosmium) to stage OSM extracts") from e

    lookup: dict[tuple[str, str], list[str]] = {}
    for group, g in catalog.poi_groups.items():
        for tag in g["tags"]:
            k, _, v = tag.partition("=")
            lookup.setdefault((k, v), []).append(group)

    pois: list[dict] = []
    node_ids: list[int] = []
    node_lon: list[float] = []
    node_lat: list[float] = []
    eu: list[int] = []
    ev: list[int] = []
    elen: list[float] = []
    ehw: list[str] = []
    seen_nodes: set[int] = set()

    def poi_groups(tags) -> list[str]:
        if tags.get("access") in ("private", "no"):
            return []
        if any(k.startswith(LIFECYCLE) for k, _ in tags):
            return []
        hits: list[str] = []
        for k, v in tags:
            hits += lookup.get((k, v), [])
        return sorted(set(hits))

    def walkable(tags) -> bool:
        hw = tags.get("highway")
        if hw is None or hw in NO_WALK_HIGHWAYS:
            return tags.get("foot") in ("yes", "designated") and hw is not None
        if hw not in WALK_HIGHWAYS:
            return False
        if tags.get("foot") == "no" or tags.get("access") in ("private", "no"):
            return tags.get("foot") in ("yes", "designated")
        return True

    class H(osmium.SimpleHandler):
        def node(self, n):
            if not n.tags:
                return
            groups = poi_groups(n.tags)
            if groups and n.location.valid():
                pois.append({"osm_id": f"n{n.id}", "lon": n.location.lon, "lat": n.location.lat,
                             "name": n.tags.get("name"), "groups": groups})

        def way(self, w):
            tags = w.tags
            groups = poi_groups(tags)
            coords = [(nd.ref, nd.lon, nd.lat) for nd in w.nodes if nd.location.valid()]
            if groups and coords:
                pois.append({"osm_id": f"w{w.id}", "lon": float(np.mean([c[1] for c in coords])),
                             "lat": float(np.mean([c[2] for c in coords])),
                             "name": tags.get("name"), "groups": groups})
            if walkable(tags) and len(coords) > 1:
                factor = 1.5 if tags.get("highway") == "steps" else 1.0
                for (a, alon, alat), (b, blon, blat) in zip(coords, coords[1:], strict=False):
                    for nid, lon, lat in ((a, alon, alat), (b, blon, blat)):
                        if nid not in seen_nodes:
                            seen_nodes.add(nid)
                            node_ids.append(nid)
                            node_lon.append(lon)
                            node_lat.append(lat)
                    eu.append(a)
                    ev.append(b)
                    elen.append(_haversine(alon, alat, blon, blat) * factor)
                    ehw.append(tags.get("highway"))

    H().apply_file(str(raws[0]), locations=True)
    pdf = pd.DataFrame(pois, columns=["osm_id", "lon", "lat", "name", "groups"])
    pdf.to_parquet(out / "pois.parquet", index=False)
    pd.DataFrame({"id": np.array(node_ids, dtype=np.int64), "lon": node_lon, "lat": node_lat}).to_parquet(
        out / "walk_nodes.parquet", index=False)
    pd.DataFrame({"u": np.array(eu, dtype=np.int64), "v": np.array(ev, dtype=np.int64),
                  "length_m": np.array(elen, dtype=np.float32), "highway": ehw}).to_parquet(
        out / "walk_edges.parquet", index=False)
    return ["pois.parquet", "walk_nodes.parquet", "walk_edges.parquet"]


def stage_tiles(src: Source, raws, out: Path, tmp: Path, catalog: Catalog) -> list[str]:
    for r in raws:
        shutil.copyfile(r, out / r.name)
    return [r.name for r in raws]


def stage_api(src: Source, raws, out: Path, tmp: Path, catalog: Catalog) -> list[str]:
    return stage_table(src, raws, out, tmp, catalog)


def _haversine(lon1, lat1, lon2, lat2) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


STAGERS = {
    "vector": stage_vector,
    "raster": stage_raster,
    "table": stage_table,
    "api": stage_api,
    "tiles": stage_tiles,
    "estv": stage_estv,
    "gtfs": stage_gtfs,
    "osm_pbf": stage_osm,
}
