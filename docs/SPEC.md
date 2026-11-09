# Qualimap — Technical Specification

Status: Draft v0.2 · 2026-11-09 (v0 implementation in this repository; Section 14 lists where it deviates from earlier drafts)
Scope: Switzerland first; grid, formats and catalog designed to extend to Europe and beyond.

Qualimap publishes quality-of-life maps on the **A5** discrete global grid. A scheduled
Python pipeline fetches open data, converts it to A5 cell indicators, and pushes versioned,
immutable outputs to an S3-compatible object store (Backblaze B2). A static web viewer reads
those outputs directly from the bucket: **GeoParquet** for tabular and SQL queries, and
**GeoZarr** (Zarr v3 with the DGGS convention) for array access. There is no application server.

---

## 1. Goals and non-goals

**Goals**

1. Every indicator is a column on a single A5 cell table; every coarser view is derived from it.
2. Data sources and indicators are declared in YAML (`catalog/indicators.yaml`). Adding a
   source or indicator needs a YAML entry plus, at most, one Python transform function.
3. The pipeline is idempotent and incremental: a source is only re-fetched when it is due,
   and only re-processed when its upstream checksum changed.
4. Outputs are static files that a browser can query with HTTP range requests.
5. One CLI (`qm`) drives everything: listing, ingesting, reingesting, building, publishing,
   maintenance, and an interactive Python prompt.

**Non-goals (v1)**

- No server-side API, no user accounts, no database server.
- No real-time data; the fastest refresh cadence is daily.
- No routing service at runtime; network distances are precomputed.

---

## 2. Architecture overview

```text
               catalog/indicators.yaml  (sources + indicators, versioned in git)
                              │
   ┌──────────────────────────┼─────────────────────────────────────────────┐
   │ qm run --due  (scheduled: GitHub Actions cron → later Dagster/Prefect)  │
   │                                                                        │
   │  1 FETCH        2 STAGE           3 CELLIFY          4 SCORE   5 EXPORT  │
   │  http/stac/     raw → normalised  join to A5 res 16  normalise GeoParquet│
   │  api/pbf        Parquet/COG       + roll-up 15..11   + weight  GeoZarr   │
   └──────┬───────────────┬─────────────────┬────────────────┬────────┬─────┘
          ▼               ▼                 ▼                ▼        ▼
   b2://qualimap-data/raw/…  staged/…   cells/…        indicators/…  releases/<v>/…
                                                                        │
                                                    CDN (cache, CORS)   │
                                                                        ▼
                          viewer: deck.gl (static HTML v0 → Next.js v1)
                          A5ParquetLayer · A5ZarrLayer · ZarrLayer · GeoArrow layers
```

| Stage | Input | Output | Engine |
| --- | --- | --- | --- |
| 1 Fetch | Upstream URL / STAC / API | Byte-identical copy in `raw/` + fetch record | `httpx`, `pystac-client`, custom fetchers |
| 2 Stage | `raw/` | Normalised Parquet (vectors), COG (rasters) in `staged/` | DuckDB + `spatial`, GDAL/rasterio, osmium |
| 3 Cellify | `staged/` + cell universe | Per-source cell values at A5 res 16 in `cells/` | DuckDB + `a5` extension, `pya5`, rasterio/exactextract |
| 4 Score | `cells/` | Raw value + normalised score per indicator in `indicators/` | Python transforms declared in YAML |
| 5 Export | `indicators/` | GeoParquet + GeoZarr release in `releases/<version>/` | DuckDB, `zarr` 3.x, `xdggs` |

Every stage writes a JSON **run record** (inputs with checksums, parameters, code version,
outputs with checksums). A stage is skipped when an identical run record already exists.

---

## 3. The A5 grid

### 3.1 Why A5

A5 partitions the globe into pentagonal cells on a dodecahedron. Properties that matter here:

- **Exactly equal area at every resolution.** Area-weighted means become plain means, and
  per-cell counts are directly comparable across the country (no latitude bias).
- **64-bit integer IDs, frozen since v1.0.** An ID written today refers to the same place in
  future library versions.
- **Hierarchy on a space-filling curve.** The descendants of a cell occupy a contiguous ID
  range at any finer resolution (up to 29). Sorting a table by `cell_id` therefore clusters
  space, and "all cells inside parent P" is a single `BETWEEN lo AND hi` range scan. This is
  the property the browser query design (Section 6) relies on.
- **Ecosystem:** official TypeScript, Python (`pya5`) and Rust libraries; a DuckDB community
  extension (`a5`); a deck.gl `A5Layer`; R and QGIS integrations.
- Aperture 4: each cell has exactly 4 children.

### 3.2 Resolutions

Values below come from `pya5` 1.0.0 (`a5.cell_area`, `a5.cell_edge_length_avg`); the cell
counts divide Switzerland's area (41,285 km²) by the cell area.

| A5 res | Cell area | Avg edge | Cells in CH | Role |
| --- | --- | --- | --- | --- |
| 11 | 8.11 km² | 2,338 m | ~5,100 | National overview; partition key for files and chunks |
| 12 | 2.03 km² | 1,169 m | ~20,400 | Regional view |
| 13 | 0.507 km² | 585 m | ~81,500 | Municipal view |
| 14 | 0.127 km² | 292 m | ~326,000 | Neighbourhood view |
| 15 | 31,669 m² | 146 m | ~1.30 M | Street-block view |
| **16** | **7,917 m²** | **73 m** | **~5.21 M** | **Base resolution: every computation happens here** |

**Base resolution 16** is chosen because its cell (0.79 ha) is just finer than the FSO
100 m population hectare grid (1 ha), so population can be assigned without coarsening.
Resolution 17 (0.2 ha, ~21 M cells) is supported by the code but not built by default.

Commune polygons (swissBOUNDARIES3D) are an additional, non-A5 aggregation level.

### 3.3 Grid operations used

The DuckDB extension is the primary engine for grid work: it covers every operation the
pipeline needs, including polygon coverings and metric-radius neighbourhoods, so most stages
are SQL over Parquet. `pya5` is used where Python is already in the loop (raster batches,
tests), and `a5-js` in the browser.

| Operation | DuckDB (`a5` ext.) | Python (`pya5`) | Browser (`a5-js`) |
| --- | --- | --- | --- |
| Point → cell | `a5_lonlat_to_cell(lon, lat, res)` | `lonlat_to_cell((lon, lat), res)` | `lonLatToCell([lon, lat], res)` |
| Cell → centre | `a5_cell_to_lonlat`, `a5_cell_to_point` (GEOMETRY) | `cell_to_lonlat` | `cellToLonLat` |
| Cell → polygon | `a5_cell_to_boundary` (ring), `a5_cell_to_geometry` (GEOMETRY, WKB) | `cell_to_boundary` | `cellToBoundary(cell, {closedRing, segments})` |
| Geometry → cells | `a5_geometry_to_cells(geom, res, overlapping := true)` | `polygon_to_cells` | — |
| Parent / children | `a5_cell_to_parent(cell, res)`, `a5_cell_to_children(cell, res)` | `cell_to_parent`, `cell_to_children` | `cellToParent`, `cellToChildren` |
| Neighbours (k steps) | `a5_grid_disk(cell, k)`, `a5_grid_disk_vertex(cell, k)` | `grid_disk`, `grid_disk_vertex` | — |
| Metric radius | `a5_spherical_cap(cell, radius_m)` | `spherical_cap` | — |
| Coverings | `a5_compact`, `a5_uncompact`, `a5_union`, `a5_intersect`, `a5_difference`, `a5_contains` | same names | — |
| Cell area / edge | `a5_cell_area(res)`, `a5_cell_edge_length_avg(res)` | `cell_area`, `cell_edge_length_avg` | — |
| Hex ⇄ u64 | `a5_u64_to_hex`, `a5_hex_to_u64` | `u64_to_hex`, `hex_to_u64` | `u64ToHex`, `hexToU64` |

Rules that follow from the A5 and extension documentation:

- **Argument order is longitude, latitude** everywhere (GeoJSON order). Set
  `SET geometry_always_xy = true` in every DuckDB session.
- **Version pinning is mandatory.** A5 1.0 changed IDs at resolution 2 and finer. DuckDB
  versions before 1.5.6 install a build with the legacy v0 IDs. Install per DuckDB version:
  - DuckDB 1.5.6+: `INSTALL a5 FROM community; LOAD a5;`
  - DuckDB 2.0+: add the Query.Farm repository, then `INSTALL a5 FROM query_farm; LOAD a5 FROM query_farm;`
  - After any DuckDB upgrade: `FORCE INSTALL`. `qm doctor` checks a set of fixed test points
    and fails if the DuckDB extension, `pya5` and `a5-js` return different IDs.
- **Coverings carry a marker.** `a5_geometry_to_cells` for polygons and `a5_compact` return a
  compacted covering whose last element is a compaction marker, not a cell. Always
  `a5_uncompact` before unnesting, joining or drawing.
- **Cell area** comes from `a5_cell_area(res)` (exact, identical for all cells at a
  resolution), never from boundary geometry.
- **Set operations** (`a5_union`, `a5_contains`, …) require both inputs at the same
  resolution.
- **JavaScript IDs** are `bigint`. Hex strings are used only where JSON must carry an ID
  (`catalog.json`, URL fragments); binary paths keep `BigUint64Array` (Parquet `UINT64`).

### 3.4 Cell universe

1. Load the national boundary (swissBOUNDARIES3D `TLM_LANDESGEBIET`, `ICC = 'CH'`), densify
   to 50 m segments in LV95, reproject to WGS84.
2. Cover it in DuckDB and expand the covering:

   ```sql
   SET geometry_always_xy = true;
   CREATE TABLE universe AS
   SELECT unnest(a5_uncompact(a5_geometry_to_cells(geom, 16, overlapping := true))) AS cell_id
   FROM boundary;   -- ~5.21 M cells
   ```

   `overlapping := true` keeps border cells whose centre lies outside but which touch the
   boundary; `in_ch_share` (below) records how much of each lies inside. For memory headroom
   the same query can run per res-11 parent (covering of boundary ∩ parent).
3. Store as `cells/universe_r16.parquet`, sorted by `cell_id`, with columns `cell_id UINT64`,
   `parent_r11..r15 UINT64`, `lon`, `lat` (centroid, float64), `in_ch_share` (share of cell
   area inside the boundary, 0–1), `is_water` (bool), `commune_bfs` (UINT16, majority commune).
4. CI asserts the universe is reproducible: same boundary release ⇒ same sorted-ID hash.

### 3.5 Joins to the grid

- **Points** (GWR buildings, POIs, stops): `a5_lonlat_to_cell(lon, lat, 16)` in DuckDB after
  `ST_Transform` from EPSG:2056 to EPSG:4326 (`always_xy := true`).
- **Rasters** (noise, air): zonal statistics of each cell polygon with `exactextract`,
  batched per res-11 parent so each batch reads one raster window. Population-weighted
  sampling at building points, where an indicator needs it, follows the VivaMap methodology.
- **Polygons** (communes, noise contours, quality classes): `a5_geometry_to_cells(geom, 16)`
  (centre containment, the default) assigns interior cells in one pass; cells from the
  `overlapping := true` covering that are not in the default covering are boundary cells, and
  only those get exact area shares via `ST_Intersection(a5_cell_to_geometry(cell_id), geom)`.
- **Neighbourhoods** (for example green share within 300 m). Two methods, chosen per
  indicator with `neighbourhood.method` in the catalog:

  | Method | Call | Selects | Strength |
  | --- | --- | --- | --- |
  | `spherical_cap` (default) | `a5_spherical_cap(cell_id, radius_m)` | Cells whose centre lies within `radius_m` | Closest to a true circle; area matches π·r² |
  | `grid_disk` | `a5_grid_disk(cell_id, k)` | Cells within k edge steps | Topological: integer, symmetric, stable across builds; natural for adjacency, smoothing and gap filling |
  | `grid_disk_vertex` | `a5_grid_disk_vertex(cell_id, k)` | Cells within k vertex steps (also corner neighbours) | Rounder than edge steps at the same k |

  For `grid_disk*`, k is either given explicitly or derived from `radius_m`: the pipeline
  picks the k whose **equivalent radius** √(n·A/π) (n = cells in the disk, A = cell area) is
  closest to `radius_m`. Measured at res 16 with `pya5` 1.0.0 (mean over five Swiss locations):

  | Neighbourhood at res 16 | Cells | Equivalent radius | Farthest centre |
  | --- | --- | --- | --- |
  | `grid_disk` k = 2 | 17 | 207 m | 224 m |
  | `grid_disk` k = 3 | 33 | 288 m | 337 m |
  | `grid_disk` k = 4 | 54 | 369 m | 447 m |
  | `grid_disk_vertex` k = 2 | 23 | 241 m | 273 m |
  | `grid_disk_vertex` k = 3 | 47 | 344 m | 410 m |
  | `spherical_cap` r = 300 m | 37 | 305 m | 292 m |
  | Ideal 300 m circle | 35.7 | 300 m | 300 m |

  So 300 m maps to `grid_disk` k = 3 (eq. radius 288 m). The disk is less round than the
  cap: its farthest centre is 17% beyond its equivalent radius versus 4% inside for the cap.
  `qm qa run --compare-neighbourhoods <indicator>` computes the indicator both ways and
  reports the per-cell and per-commune differences, so the choice can be made on data.
- Joining on `cell_id` replaces geometric predicates everywhere after this step; the
  extension authors note that probing an indexed cell column is much cheaper than
  `ST_DWithin` or `ST_Intersects` on raw geometries.

### 3.6 Roll-up

Coarser resolutions are always computed from res 16 (never from the previous level):

```text
value(parent) = Σ w_c · value(c) / Σ w_c   over res-16 descendants c with data
```

`w_c` is the cell population for population-weighted indicators, or 1 for area-weighted
indicators (A5 cells are equal-area). Communes use the GWR commune number of each building
for population weights.

---

## 4. Object storage layout (Backblaze B2)

### 4.1 Buckets and access

| Bucket | Visibility | Contents |
| --- | --- | --- |
| `qualimap-data` | Private | `raw/`, `staged/`, `cells/`, `indicators/`, `state/`, `runs/` |
| `qualimap-pub` | Public read | `releases/<version>/…`, `releases/latest.json` |

Access goes through B2's S3-compatible API, so all tooling uses generic S3 clients
(`fsspec`/`s3fs` in Python, `rclone` for bulk ops). Configuration comes from the
environment only:

```text
QM_S3_ENDPOINT=https://s3.<region>.backblazeb2.com
QM_S3_REGION=<region>
QM_S3_KEY_ID=…            # application key restricted to the two buckets
QM_S3_SECRET=…
QM_BUCKET_DATA=qualimap-data
QM_BUCKET_PUB=qualimap-pub
QM_PUBLIC_BASE_URL=https://data.qualimap.<tld>   # CDN in front of qualimap-pub
```

The public bucket gets a CORS rule allowing `GET` and `HEAD` with the `Range` header from
the viewer origins, and exposing `Content-Length`, `Content-Range`, `ETag` and
`Accept-Ranges`. A CDN sits in front of the bucket; check B2's current egress and CDN-partner
terms when choosing it. Swapping B2 for another S3 store (R2, MinIO, AWS S3) changes only
these variables.

### 4.2 Key layout

```text
qualimap-data/
  raw/<source_id>/<fetched_at:YYYYMMDDTHHMMSSZ>/<original_filename>
  staged/<source_id>/<content_sha256>/<name>.parquet | .tif
  cells/universe_r16.parquet
  cells/<source_id>/<content_sha256>/r16.parquet
  indicators/<indicator_id>/<inputs_hash>/r16.parquet
  state/sources/<source_id>.json          # last fetch: url, etag, sha256, bytes, fetched_at
  runs/<YYYY>/<MM>/<run_id>.json          # run records

qualimap-pub/
  releases/<version>/manifest.json
  releases/<version>/parquet/cells_r16.parquet
  releases/<version>/parquet/cells_r{11..15}.parquet
  releases/<version>/parquet/communes.parquet
  releases/<version>/parquet/pois.parquet
  releases/<version>/arrow/communes.arrow          # same tables as Arrow IPC with interleaved
  releases/<version>/arrow/pois.arrow              #   GeoArrow, for the viewer (Section 6.3)
  releases/<version>/zarr/qualimap.zarr/…          # A5 cell indicators (DGGS convention)
  releases/<version>/zarr/rasters/<source_id>.zarr/…  # raster context layers (GeoZarr)
  releases/<version>/catalog.json         # resolved catalog: indicators, labels, legends
  releases/latest.json                    # {"version": "2026.11.1", "manifest": "…"}
```

- Everything under `releases/<version>/` is **immutable** and served with
  `Cache-Control: public, max-age=31536000, immutable`. Only `latest.json` is mutable
  (`max-age=300`).
- Version scheme: `YYYY.MM.N` (calendar release). The manifest lists every file with size and
  SHA-256, the catalog version (git SHA), the A5 library version, and each source's upstream
  checksum, so any release can be reproduced.
- Retention: keep `raw/` for the last 3 fetches per source, and every published release.
  `qm gc` enforces this.

---

## 5. Pipeline and workflow

### 5.1 Scheduling

Each source declares a `refresh` policy in YAML (Section 7). One scheduled entry point decides
what is due:

```text
qm run --due            # fetch due sources → rebuild affected indicators → export → publish
```

- **Phase 1:** a GitHub Actions workflow (`.github/workflows/pipeline.yml`) runs
  `qm run --due` daily at 02:17 UTC: it installs the pipeline package, runs `qm doctor`, and
  treats exit code 3 ("nothing due") as success. Bucket settings and upstream URLs come from
  repository variables, credentials from secrets (template: `.env.example`). Heavy national
  jobs (OSM routing, raster zonal stats) can target a self-hosted runner using the image from
  `docker/pipeline.Dockerfile`.
- **Phase 2:** the same image runs under Dagster (or Prefect): each catalog source becomes an
  asset, each indicator an asset depending on its inputs. The CLI stays the single
  implementation; the orchestrator only calls it.

### 5.2 Dependency graph and incrementality

The catalog defines a DAG: `source → indicator → dimension → release`. On each run:

1. For every due source, `fetch` compares the upstream ETag, STAC `checksum:multihash` or
   SHA-256 with `state/sources/<id>.json`. Unchanged ⇒ stop for that source.
2. Changed sources mark their downstream indicators dirty (transitively).
3. Only dirty indicators recompute; the cache key is `inputs_hash` = hash(input checksums,
   transform name and version, parameters, A5 version, base resolution).
4. If any indicator changed, `export` builds a new release; `publish` uploads it and then
   flips `latest.json` as the last step (atomic from the reader's point of view).

### 5.3 Failure handling

- Fetch retries 3× with exponential backoff; a source that fails keeps its previous data and
  is flagged `stale` in the release manifest.
- Every indicator has QA checks (Section 9). A failed check blocks publication of the release
  but not of the run record, so the failure can be inspected.
- Notifications: failures post to a webhook (`QM_ALERT_WEBHOOK`) with the run ID.
- Exit codes of `qm run`: 0 published, 1 failed, 2 QA blocked the `latest.json` switch,
  3 nothing to do.
- Sources whose fetch needs staged data from another source (the ESTV tax API needs the
  commune register to enumerate communes) are fetched after the other sources have been
  staged, in the same run.

---

## 6. Output formats

The cell table is published twice from the same data: GeoParquet is the **source of truth**
for tabular and SQL access; GeoZarr is a derived, array-oriented view.

### 6.1 GeoParquet (`parquet/cells_r{11..16}.parquet`)

**Schema (one row per A5 cell):**

| Column | Type | Notes |
| --- | --- | --- |
| `cell_id` | UINT64 | A5 ID, primary key, **sort key** |
| `geometry` | WKB Point | Cell centre from `a5_cell_to_point(cell_id)`. Polygons are derived client-side from `cell_id`, keeping the file small |
| `bbox` | STRUCT(xmin, ymin, xmax, ymax FLOAT) | GeoParquet 1.1 bbox covering column, for clients without A5 support |
| `commune_bfs` | UINT16 | Majority commune (BFS number) |
| `pop` | FLOAT32 | Residents (dasymetric estimate) |
| `q_<indicator>` | UINT8 | Score 0–250 (= score × 25); 255 = no data |
| `raw_<indicator>` | FLOAT32 | Raw value in the unit declared in the catalog |
| `flags` | UINT32 | Bit flags (stale source, limit exceeded, …), documented in `catalog.json` |

**Physical layout** (this is what makes browser queries cheap):

- Rows sorted by `cell_id`, so A5's contiguous descendant ranges map to contiguous row ranges.
- One row group per **tile**: the ancestor at the tile resolution `max(r − 5, 0)` (res 11 for
  res 16), so a row group holds the ≤ 1,024 descendants of one tile cell, ~5,100 row groups of
  ~1,000 rows at res 16. Row group min/max statistics on `cell_id` identify each group's tile
  without an extra index. Parquet page index enabled. The tile resolutions per file are listed
  in `catalog.json` (`tile_resolutions`).
- ZSTD level 9; dictionary encoding for `q_*` and `commune_bfs`.
- Footer metadata: `geo` (GeoParquet 1.1, CRS OGC:CRS84), plus `qualimap` key with release
  version, A5 resolution and A5 library version.

Estimated size of `cells_r16.parquet`: 5.2 M rows × ~30 bytes compressed ≈ 150 MB (to be
measured). Coarser files are a few MB each.

**Browser query paths:**

| Path | Library | Use |
| --- | --- | --- |
| Viewport fetch | `hyparquet` + `hyparquet-compressors` (ZSTD) | Read footer once, map tile → row group from the `cell_id` statistics, fetch only the needed columns of the tiles in view |
| Ad-hoc SQL | DuckDB-WASM + `a5` community extension | `SELECT … FROM 'https://…/cells_r16.parquet' WHERE cell_id BETWEEN ? AND ?`; commune rankings, filters, histograms |

DuckDB-WASM loads community extensions in the browser; whether the `a5` extension's WASM
build works end to end must be confirmed in the first viewer spike. Range filtering works
without it, because IDs and ranges can be computed with `a5-js`.

**Viewport → tiles:** A5 cells are hierarchical by ID but **not geometrically nested**: the
descendants of a cell spill well outside its own pentagon (a res-7 cell's res-12 descendants
reach ~0.4° beyond it). The tiles for a viewport are therefore the tile-resolution parents of
the data-resolution cells covering the viewport (`polygonToCells` at r, then `cellToParent`),
not the tile-resolution cells overlapping it. Each tile is then one row group.

### 6.2 GeoZarr (`zarr/qualimap.zarr`, Zarr v3)

DGGS data is stored as 1-D arrays indexed by cell, following the Zarr **DGGS convention**
(part of the GeoZarr convention set, currently at "Proposal" maturity) and readable by
`xdggs`:

```text
qualimap.zarr/
  zarr.json                       # attrs: zarr_conventions [dggs, multiscales], qualimap {…}
  r16/
    zarr.json                     # attrs: dggs {name: "a5", refinement_level: 16, …}
    cell_ids     uint64 [N16]     chunks 65,536
    q_noise      uint8  [N16]     chunks 65,536, zstd
    …            (one array per indicator, raw_* as float32)
    tile_ids     uint64 [T]       # tile cells (res r − 5), sorted
    tile_index   uint64 [T, 2]    # per tile: [first_row, row_count]
  r15/ … r11/                     # same structure
```

Example DGGS attributes on `r16/zarr.json`:

```json
{
  "zarr_conventions": [{
    "uuid": "7b255807-140c-42ca-97f6-7a1cfecdbc38",
    "name": "dggs",
    "schema_url": "https://raw.githubusercontent.com/zarr-conventions/dggs/refs/tags/v1/schema.json"
  }],
  "dggs": {
    "name": "a5",
    "refinement_level": 16,
    "spatial_dimension": "cells",
    "coordinate": "cell_ids",
    "ellipsoid": {"name": "wgs84", "semimajor_axis": 6378137.0, "inverse_flattening": 298.257223563}
  }
}
```

- **Gap to close:** published DGGS-convention examples use `healpix` and `h3`; an A5 entry
  (name and indexing scheme) is not yet documented there. Qualimap writes `name: "a5"`
  and should propose it upstream (zarr-conventions/dggs, xdggs).
- `tile_ids` + `tile_index` give the viewer direct access to any tile's rows without reading
  `cell_ids` first: one small read per resolution, then one chunk-range read per array.
- Browser access with `zarrita.js` over HTTP range requests.

**When to use which:** the viewer uses Parquet for viewport data and attribute panels, and
Zarr for single-indicator layers at high zoom (one uint8 array, no row decoding). Analysts use
Zarr with xarray/xdggs and Parquet with DuckDB, pandas or QGIS.

### 6.3 Communes

`parquet/communes.parquet`: one row per commune with simplified polygon geometry (GeoParquet
1.1, WKB, CRS84, bbox covering column), `commune_bfs`, `name`, `canton`, `pop`, and `q_*` /
`raw_*` columns. For the viewer the same table is also written as
`arrow/communes.arrow`: Arrow IPC with **interleaved GeoArrow** multipolygons, which deck.gl's
GeoArrow layers take without any decoding in the browser (Section 10.6).

### 6.4 Raster GeoZarr context layers (`zarr/rasters/<source_id>.zarr`)

Selected source rasters (sonBASE noise day and night, NO₂, PM10, PM2.5) are republished as
raster GeoZarr so the viewer can overlay the continuous source field under the A5 scores:

- Zarr v3, one group per source, `multiscales` convention with a 2× overview pyramid down to
  ~1 km pixels, `geo-proj` (`proj:code` EPSG:2056, the native grid) and `spatial` (affine
  transform) conventions, so `ZarrLayer` can select levels and reproject to web mercator.
- Chunks 512 × 512, zstd, float32 (or uint8 with `scale_factor` for noise dB).
- Clipped to the national boundary; built by `qm export --formats raster-zarr`.
- The catalog declares which sources get a raster export (`export.raster_zarr: true` on the
  source entry).

### 6.5 Points of interest (`parquet/pois.parquet`)

The OSM POIs used by the access indicators, one row per POI: point geometry (WKB, CRS84),
`group` (catalog `osm_poi_groups` key), `dimension`, `name`, `osm_id`, `cell_id_r16`.
Sorted by `cell_id_r16`. Also written as `arrow/pois.arrow` (interleaved GeoArrow points) and
rendered with `GeoArrowScatterplotLayer` at high zoom.

---

## 7. Catalog: `catalog/indicators.yaml`

The catalog is the single declaration of what Qualimap computes. It has four top-level
sections; `catalog/schema/indicators.schema.json` validates it (`qm catalog validate`).

| Section | Purpose |
| --- | --- |
| `meta` | Catalog version, base resolution, default region, languages |
| `dimensions` | User-facing groups (Noise, Air, …) with labels, default weights, colour ramps |
| `sources` | Upstream datasets: provider, licence, fetch method, refresh policy, formats |
| `indicators` | Computed layers: inputs, transform, aggregation, normalisation, QA checks |

### 7.1 Source entry

```yaml
- id: foen_sonbase_road_day
  provider: FOEN
  title: sonBASE road traffic noise, day
  layer_id: ch.bafu.laerm-strassenlaerm_tag
  license: {id: OGD-CH-BY, attribution: "© BAFU"}
  fetch:
    method: stac               # stac | http | estv_api | osm_pbf | gtfs | custom
    collection: ch.bafu.laerm-strassenlaerm_tag
    asset_media_type: image/tiff
  refresh: {every: P30D, check: checksum}
  kind: raster                 # raster | vector | table | api
  crs: EPSG:2056
  export: {raster_zarr: true}  # also publish as raster GeoZarr for the viewer (Section 6.4)
```

### 7.2 Indicator entry

```yaml
- id: noise
  dimension: noise
  inputs: [foen_sonbase_road_day, foen_sonbase_road_night, foen_sonbase_rail_day,
           foen_sonbase_rail_night, foca_aerodrome_noise, fso_gwr, fso_statpop]
  transform: {fn: qualimap.transforms.noise:effective_level, version: 1}
  unit: dB(A)
  aggregation: pop_weighted_energetic_mean
  normalize: {method: linear, good: 45, bad: 65}
  qa: [{check: nodata_share_settlement, max: 0.01}]
```

### 7.3 Extension mechanism

- A new source needs a YAML entry. If its `fetch.method` is not built in, add a fetcher class
  registered under the Python entry point group `qualimap.fetchers`.
- A new indicator needs a YAML entry and a transform function `fn(ctx, inputs, params) ->
  pyarrow.Table[cell_id, value]` registered under `qualimap.transforms` (or referenced by
  dotted path as above).
- Additional catalog files can be layered: `qm --catalog catalog/indicators.yaml --catalog
  catalog/local/*.yaml`; later files override entries with the same `id`.
- Values of the form `${NAME}` are resolved from the environment at load time, so URLs that
  change per release (for example the GWR extract) live in `.env`, not in git.
- Lookup tables referenced by transforms live under `catalog/maps/` (for example
  `nolc04_classes.csv`, mapping NOLC04 categories to sealed / green / tree / excluded).
- A breaking change to a transform increments its `version`, which changes `inputs_hash` and
  forces recomputation.

---

## 8. Command-line interface (`qm`)

Built with Typer. Global options: `--catalog PATH` (repeatable), `--env FILE`,
`--json` (machine-readable output), `--dry-run`, `-v/--verbose`, `--region CH`.

### 8.1 Catalog and status

| Command | Purpose | Key parameters |
| --- | --- | --- |
| `qm sources list` | Table of all sources with provider, method, refresh policy, last fetch, status | `--provider`, `--stale`, `--due` |
| `qm sources show <id>` | Full YAML entry, last fetch record, downstream indicators | |
| `qm indicators list` | All indicators with dimension, inputs, last build, release value range | `--dimension` |
| `qm indicators show <id>` | Definition, lineage, last QA results | |
| `qm catalog validate` | Validate YAML against the JSON Schema and check the DAG (unknown IDs, cycles) | |
| `qm catalog graph` | Print the dependency graph | `--format mermaid\|dot` |
| `qm status` | Due sources, dirty indicators, latest release, last run outcome | |

### 8.2 Pipeline

| Command | Purpose | Key parameters |
| --- | --- | --- |
| `qm fetch <id…>` | Fetch sources if upstream changed | `--all`, `--due`, `--force` |
| `qm reingest <id…>` | Force fetch + stage + cellify for sources, then mark downstream dirty | `--all`, `--from-raw` (re-stage from existing raw, no download) |
| `qm stage <id…>` | Normalise raw files | `--force` |
| `qm cells build` | Build or rebuild the cell universe | `--res 16`, `--boundary-release` |
| `qm compute <indicator…>` | Compute indicators | `--all`, `--dirty`, `--force`, `--res`, `--bbox lon1,lat1,lon2,lat2` (local test runs) |
| `qm export` | Write GeoParquet, A5 GeoZarr and raster GeoZarr for a release | `--version`, `--formats parquet,zarr,raster-zarr`, `--res 11-16` |
| `qm publish <version>` | Upload a release and switch `latest.json` | `--no-switch` |
| `qm run` | End-to-end orchestration | `--due` (default), `--all`, `--until stage\|cellify\|compute\|export\|publish` |

### 8.3 Maintenance

| Command | Purpose | Key parameters |
| --- | --- | --- |
| `qm releases list` | Published releases with size and source versions | |
| `qm releases rollback <version>` | Point `latest.json` at an earlier release | |
| `qm releases diff <a> <b>` | Per-indicator change statistics between two releases | `--commune <bfs>` |
| `qm qa run` | Run QA checks without publishing | `--indicator`, `--compare-neighbourhoods` |
| `qm inspect cell <cell_id\|lon,lat>` | All indicator values and lineage for one cell | `--res` |
| `qm inspect commune <bfs\|name>` | Commune values and ranks | |
| `qm gc` | Apply retention rules to `raw/`, `staged/`, `cells/`, `indicators/` | `--keep-raw 3`, `--dry-run` |
| `qm cors apply` | Apply the CORS rules to the public bucket | |
| `qm doctor` | Check credentials, bucket access, CORS, DuckDB ≥ 1.5.6 with A5 v1 extension, and identical test-vector IDs across DuckDB, `pya5` and `a5-js` | |
| `qm shell` | Interactive Python prompt (IPython) with `catalog`, `store`, `duck` (DuckDB connection with `spatial` + `a5` loaded) and `a5` preloaded | `--release <version>` |
| `qm demo` | Publish a synthetic release in the production layout (all formats, ~9 communes) for viewer development and the JS tests; no network needed | `--bbox`, `--res`, `--no-switch` |

Example session:

```text
$ qm sources list --due
ID                        PROVIDER  METHOD   REFRESH  LAST FETCH          STATUS
osm_switzerland           Geofabrik osm_pbf  P7D      2026-10-31T02:17Z   due
fso_gwr                   FSO       http     P30D     2026-10-15T02:20Z   ok
…

$ qm reingest foen_no2 --from-raw
$ qm compute air --force --bbox 8.4,47.3,8.7,47.45
$ qm shell
>>> duck.sql("select count(*) from cells where q_air < 50").show()
```

Exit codes: 0 success, 1 failure, 2 QA failure, 3 nothing to do (for `--due`).

---

## 9. Quality assurance

- **Catalog:** JSON Schema validation and DAG checks on every pull request.
- **Code:** unit tests per transform on small fixtures (a few res-11 parents around Zürich,
  Lausanne and Lugano); `ruff`, `mypy`.
- **Data contracts:** each indicator's `qa` list runs after compute. Built-in checks include
  `range`, `nodata_share`, `nodata_share_settlement`, `count_delta_vs_previous`,
  `reconcile_population` and `compare_reference`.
- **Golden values:** pinned values for named cells and communes; changes need an explicit
  update in the same pull request as the code change.
- **Release diff:** `qm releases diff` runs automatically before `publish`; changes beyond
  configured thresholds block the switch of `latest.json`.

---

## 10. Viewer (deck.gl)

The viewer is a **deck.gl application** in both phases. Every map element is a deck.gl layer
in one `layers` array; deck.gl draws them in array order, and picking, hover and visibility
work per layer. MapLibre provides only the basemap: deck.gl attaches to it through
`MapboxOverlay` (`@deck.gl/mapbox`) in interleaved mode, so data layers can sit below the
basemap's labels.

### 10.1 Layer stack

| Order | Layer id | Class | Data | Zoom | Purpose |
| --- | --- | --- | --- | --- | --- |
| 1 | `raster-<source>` | `ZarrLayer` (`@developmentseed/deck.gl-zarr`) | Raster GeoZarr, Section 6.4 | all | Optional context overlays: noise level, NO₂, PM2.5 as continuous rasters |
| 2 | `communes` | `GeoArrowPolygonLayer` (`@geoarrow/deck.gl-geoarrow`) | `arrow/communes.arrow` | < 8 | Commune choropleth of the personal score |
| 3 | `a5-parquet` | `A5ParquetLayer` (Qualimap) | `cells_r{11..16}.parquet`, Section 6.1 | ≥ 8 | **Main layer:** personal score from all nine `q_*` columns |
| 4 | `a5-zarr` | `A5ZarrLayer` (Qualimap) | `qualimap.zarr`, Section 6.2 | ≥ 8 | Single-indicator view (one uint8 array per cell); alternative to layer 3 |
| 5 | `pois` | `GeoArrowScatterplotLayer` | `arrow/pois.arrow`, Section 6.5 | ≥ 13 | Schools, doctors, stops behind the access scores |
| 6 | `selection` | `A5Layer` (`@deck.gl/geo-layers`) | Picked cells | all | Outline of hovered or selected cells |

Layers 3 and 4 show the same cells; the UI toggles one or the other ("all dimensions,
weighted" versus "one indicator"). `buildLayerStack()` in `packages/qm-layers/src/stack.js`
builds this array for both viewers, so they always draw the same stack.

**Why custom A5 layers.** `ZarrLayer` targets raster arrays: it reads the GeoZarr `spatial`,
`multiscales` and `geo-proj` conventions to derive the level pyramid, affine transforms and
CRS, streams only the chunks visible in the viewport, and reprojects them on the fly. Its
documentation does not cover 1-D DGGS cell arrays, so A5 indicators get two small
Qualimap layers built from deck.gl primitives instead. They reuse the same ideas: viewport
tiling, level selection, chunk streaming.

### 10.2 `A5TileLayer`: the shared base

Both A5 layers are deck.gl `TileLayer`s whose tiles are **A5 cells instead of web-mercator
XYZ tiles**:

- A custom `Tileset2D` subclass (`A5Tileset`, passed through `TileLayer`'s `TilesetClass`
  prop, confirmed in deck.gl 9.4) returns, for the current viewport, the **tiles**: the
  tile-resolution parents of the data cells covering the viewport (Section 6.1 explains why
  not the tile cells covering it). Tile resolution = data resolution − 5 (one tile ≤ 1,024
  data cells), so at res 16 a tile is a res-11 cell. A tile's bounding box (for culling) is
  the extent of its descendants three levels down, padded by one such descendant.
- Level selection follows the zoom table in 10.5; tiles are cached by `(release, res, tile_id)`.
- Each tile's `getTileData` returns `{cellIds: BigUint64Array, values: …}` from the format
  reader (Parquet or Zarr).
- **Rendering, two steps:**
  1. **v0 (default, `renderMode: 'a5'`):** `renderSubLayers` returns deck.gl's `A5Layer` for the tile with
     `data = {length}`, `getPentagon: (_, {index}) => cellIds[index]` (`getPentagon` accepts a
     `bigint` or a hex string) and `getFillColor` reading a precomputed RGBA buffer.
     `A5Layer` is a composite layer whose `cell` sublayer is a `PolygonLayer`; it computes
     boundaries itself, and its documentation does not describe binary-attribute input.
     deck.gl 9.4's `A5Layer` depends on `a5-js` 0.10, whose cell IDs differ from `pya5` /
     `a5-js` 1.0: the root `package.json` forces a single `a5-js` 1.0 with an npm `overrides`
     entry (only `cellToBoundary` and `hexToU64` are used, unchanged in 1.0). Without it the
     cells render at wrong locations.
  2. **v1 (fast path, `renderMode: 'binary'`, implemented on the main thread):** compute boundaries once per tile with
     `cellToBoundary(cell, {closedRing: true, segments: 1})` (6 vertices per pentagon at
     these resolutions), pack them into flat `Float32Array` positions plus `startIndices`, and
     render a `SolidPolygonLayer` with binary attributes. Weight changes then only replace the
     colour attribute. Switch when the M3 benchmark shows `A5Layer` above budget at res 16.
- Geometry is cached with the tile, so pans and weight changes never recompute it.
- When the set of columns a tile needs changes (indicators, filter columns) or the Zarr
  indicator changes, `reloadTiles()` marks all tiles for reload and runs the tileset update
  immediately; cached content stays on screen until replaced.

### 10.3 `A5ParquetLayer`

- Opens the footer of `cells_r{res}.parquet` once (hyparquet with hyparquet-compressors for
  ZSTD, range requests) and builds a map `tile → row group` from the row-group `cell_id`
  statistics (Section 6.1 writes one row group per tile).
- `getTileData` reads one row group, only the columns `cell_id`, the nine `q_*`, `pop` and
  `flags` (~20 bytes per cell before compression).
- Colour = ramp(`Σ wᵢ·qᵢ / Σ wᵢ` over indicators with qᵢ ≠ 255). Weights are a layer prop;
  `updateTriggers: {getFillColor: weights}` recolours the cached tiles without refetching.
  The colour buffer is computed per tile on the main thread (a worker is the planned next
  step if the M3 budget is missed).
- `mask` / `maskKey` props: cells that fail the user's filters (Section 10.8) are drawn as no
  data. Filters read `raw_*` columns, which the layer adds to `extraColumns` on demand.
- Arrow alternative: `parquet-wasm` → Arrow table → typed columns. Use it if the hyparquet path
  is slower in the M3 benchmark; the layer API stays the same.

### 10.4 `A5ZarrLayer`

- Opens `qualimap.zarr/r{res}/` with `zarrita`, reads `tile_ids` and `tile_index` once (two
  small arrays).
- `getTileData` for a tile: look up `[first_row, row_count]` and fetch the slice of
  `cell_ids` and of the selected `q_<indicator>` array (one or two chunks each).
- Colour = ramp(qᵢ / 25). No weighting; switching the indicator swaps the array and refetches.

### 10.5 Zoom → data resolution

MapLibre zoom at ~47° N:

| Zoom | Resolution | Source |
| --- | --- | --- |
| < 8 | Communes | `arrow/communes.arrow` (whole file, GeoArrow polygons) |
| 8–9.99 | 11 | tiles at res 11 (whole file is ~5,100 rows) |
| 10–10.99 | 12 | tiles |
| 11–11.99 | 13 | tiles |
| 12–12.99 | 14 | tiles |
| 13–13.99 | 15 | tiles |
| ≥ 14 | 16 | tiles |

### 10.6 GeoArrow and GeoParquet details

- `@geoarrow/deck.gl-geoarrow` (the renamed `@geoarrow/deck.gl-layers`, 0.4) passes Arrow
  buffers straight to deck.gl's binary attributes: the layers take an `arrow.RecordBatch`
  and per-row colours as `arrow.Data<FixedSizeList<Uint8>[4]>`. Its ES modules use
  extensionless imports, so it needs a bundler or esm.sh (not plain Node).
- **Geometry encoding.** GeoParquet 1.1 native GeoArrow encoding allows only the "separated"
  coordinate layout; deck.gl expects "interleaved", and WKB must be decoded. The pipeline
  therefore writes the two vector tables a second time as Arrow IPC with interleaved
  GeoArrow (`arrow/communes.arrow`, `arrow/pois.arrow`): no decoding in the browser and no
  `parquet-wasm`. The GeoParquet files stay the analysis format. A5 cells need neither:
  their geometry is derived from `cell_id`.
- **CRS.** `ZarrLayer` reprojects rasters itself; vector layers do not. Every GeoParquet the
  pipeline publishes is written in CRS84 (lon/lat).
- **Size.** `communes.arrow` and `pois.arrow` are loaded whole (a few MB). Cell tables are
  never loaded whole beyond res 11; they stream per tile.

### 10.7 Phase 0: single HTML page (`viewer/index.html`)

- No build step: ES modules from esm.sh with pinned versions through an import map:
  `@deck.gl/*`, `@geoarrow/deck.gl-geoarrow`, `apache-arrow`, `hyparquet`,
  `hyparquet-compressors`, `zarrita`, `a5-js`, `proj4`, `maplibre-gl`,
  `@developmentseed/deck.gl-zarr`. `a5-js` is marked external for `@deck.gl/geo-layers` so the
  page uses a single a5-js 1.0 (see 10.2).
- The layers, readers and helpers live in the workspace package `packages/qm-layers` (plain
  ES modules with JSDoc), imported by both phases. `npm run viewer` serves the page with Vite
  and local `node_modules` against a local release (`QM_VIEWER_DATA`, default `.qm/pub`).
- URL parameters: `?data=` (public bucket), `?release=`, `?style=`, `?interleaved=0`,
  `?debug`.
- UI: nine weight sliders, an indicator picker (switches to `A5ZarrLayer`), raster overlay
  toggles, a legend, and a hover tooltip with the per-indicator breakdown. State lives in the
  URL fragment (`#w=…&z=…&ll=…&ind=…`).
- Discovery: `releases/latest.json` → `manifest.json` → `catalog.json` (labels, ramps,
  default weights, layer URLs).

### 10.8 Phase 1: Next.js application (`apps/web`)

- Next.js 16 App Router with **static export** (`output: 'export'`, no server runtime); the
  map renders client-side only. Build-time settings: `QM_DATA`, `QM_BASE_PATH`,
  `QM_MAP_STYLE`, `QM_DUCKDB_EXTENSIONS`; per-visit overrides `?data=`, `?release=`,
  `?style=`, `?engine=cpu`, `?binary`.
- `react-map-gl/maplibre` basemap with deck.gl **interleaved** through `MapboxOverlay` and
  `useControl` (the deck.gl-recommended pattern for interleaving with react-map-gl;
  `@deck.gl/react`'s `DeckGL` component draws on its own canvas and cannot interleave).
- Layers, readers, release discovery, filters, analysis queries and UI strings come from
  `packages/qm-layers`, written in JavaScript with JSDoc rather than TypeScript, so the
  no-build phase-0 page and the app run the same files. Release files are immutable and
  versioned by path, so the browser HTTP cache does the caching; a separate `qm-data`
  package and Cache API layer were not needed.
- Analysis panel (opens on demand): commune rankings for the user's weights (click to zoom),
  population histogram of the personal score, "top 10 % for your weights" threshold, and the
  population living in cells that match the filters. It runs on **DuckDB-WASM** in a worker,
  reading `communes.parquet` and the cell file at the finest resolution ≤ 13 over HTTP range
  requests. The DuckDB-WASM bundles (~36–41 MB wasm) are self-hosted from the static export
  and loaded only when the panel opens. DuckDB-WASM autoloads its Parquet extension from
  `extensions.duckdb.org` unless `QM_DUCKDB_EXTENSIONS` names a self-hosted mirror. If
  DuckDB-WASM cannot start, a plain-JS engine computes the same results from the same files
  (`packages/qm-layers/src/analysis.js` holds both the SQL and the JS versions; tests and a
  DuckDB cross-check confirm they agree).
- Filters are thresholds on raw indicator values in their own units (for example "noise ≤
  55 dB(A)" and "public transport ≥ 0.5"); the same definition masks cells in
  `A5ParquetLayer` and filters rows in SQL.
- i18n de, fr, it, en (UI strings in `qm-layers`, labels from `catalog.json`); native,
  keyboard-accessible range sliders; viridis ramp.
- Performance budget: first meaningful map < 2.5 s on 4G; slider-to-repaint < 50 ms for
  60,000 visible cells. If the worker recolour path misses the budget, move the weighting into
  a deck.gl `LayerExtension` (weights as uniforms, the nine scores as instanced attributes).

---

## 11. Repository layout

```text
qualimap/
  catalog/
    indicators.yaml                 # sources, indicators, dimensions
    schema/indicators.schema.json
    maps/                           # lookup tables used by transforms (e.g. NOLC04 classes)
    local/                          # optional overlay catalogs (git-ignored or per deployment)
  docs/
    SPEC.md                         # this document
  pipeline/                         # Python package `qualimap` (pip install -e "pipeline[dev]")
    pyproject.toml
    src/qualimap/
      cli.py                        # Typer app (qm)
      settings.py, store.py         # environment, fsspec stores (local or S3/B2)
      catalog.py                    # load, layer, interpolate ${ENV}, validate, DAG
      state.py, pipeline.py         # fetch/run records; run --due orchestration, exit codes
      fetchers/                     # http, stac, estv_api, osm_pbf, gtfs, none (+ entry points)
      staging.py                    # raw → staged Parquet / COG
      grid.py, population.py        # A5 coverings, universe, joins, neighbourhoods, roll-up
      transforms/                   # noise, air, public_transport, taxation, poi_access, green, solar
      normalize.py, compute.py, qa.py
      export/                       # tables, geoparquet, geozarr, release_builder
      releases.py, maintenance.py   # publish/rollback/diff; gc, cors, doctor
      duck.py                       # DuckDB connection with spatial + a5
      demo.py                       # synthetic release (qm demo)
    tests/                          # end-to-end run on synthetic upstreams (respx) + unit tests
  packages/qm-layers/               # deck.gl layers, A5 tileset, readers, stack, filters,
                                    # analysis SQL/JS, i18n (plain ES modules, node --test)
  viewer/                           # phase 0: index.html (esm.sh import map), app.js, Vite dev config
  apps/web/                         # phase 1: Next.js static export + DuckDB-WASM panel
  .github/workflows/
    ci.yml                          # ruff, catalog validate, pytest, qm demo, JS tests, web build
    pipeline.yml                    # scheduled qm run --due
  docker/pipeline.Dockerfile
  .env.example
```

Python 3.12; key dependencies: `pya5` ≥ 1.0, `duckdb` ≥ 1.5.6 (with `spatial` and the A5 v1
`a5` extension), `pyarrow`, `geopandas`, `shapely` 2, `pyproj`, `rasterio`, `scipy`, `zarr` 3,
`fsspec` + `s3fs` (B2 through the S3 API; CORS calls through `botocore`), `httpx`, `typer`,
`rich`, `pyyaml`, `jsonschema`. Extras: `osm` (`osmium`), `zonal` (`exactextract`), `shell`
(`ipython`), `analysis` (`xarray`, `xdggs`), `dev` (`pytest`, `respx`, `ruff`).

JavaScript: deck.gl 9.4, `a5-js` 1.0 (forced for deck.gl via npm `overrides`), `hyparquet` +
`hyparquet-compressors`, `zarrita`, `apache-arrow`, `@geoarrow/deck.gl-geoarrow`,
`@developmentseed/deck.gl-zarr`, `proj4`, MapLibre GL 5; web app: Next.js 16, React 19,
`react-map-gl` 8, `@duckdb/duckdb-wasm`.

---

## 12. Roadmap

| Milestone | Deliverable | Done when |
| --- | --- | --- |
| M0 Skeleton | Repo layout, catalog + schema, `qm sources list`, `qm catalog validate`, CI | Catalog validates in CI |
| M1 Grid | A5 universe for CH, GWR + STATPOP joined, roll-up, `qm inspect cell` | Commune population reconciles within 0.1% |
| M2 First indicators | Noise, air, public transport end to end; GeoParquet export to B2 | `cells_r16.parquet` readable via HTTP range in a browser |
| M3 Viewer v0 | deck.gl page with `A5ParquetLayer`, `communes` GeoArrow layer and weight sliders; benchmark hyparquet vs parquet-wasm | Weighted map streams per tile from the public bucket |
| M4 All nine dimensions | Taxation, POI access ×4, green; A5 GeoZarr + raster GeoZarr export; `A5ZarrLayer`, `ZarrLayer` overlays, POI layer | All QA checks pass; release diff tooling |
| M5 Scheduling | GitHub Actions `qm run --due`; alerts; retention | Two consecutive unattended releases |
| M6 Web app | Next.js app with DuckDB-WASM analysis panel and i18n | Feature parity with v0 plus rankings |
| M7 Beyond CH | Region abstraction (`--region`), first non-Swiss pilot region | Catalog supports per-region sources |

---

## 13. Open questions

1. Licence for the code (MIT or Apache-2.0) and for published data (respecting upstream terms,
   e.g. OSM ODbL share-alike for OSM-derived layers, FSO "open use, must provide source").
2. Base resolution 16 versus 15: res 16 gives ~5.2 M cells (~150 MB Parquet); res 15 would
   quarter the size if street-level detail is not needed.
3. Whether the `a5` DuckDB extension runs in DuckDB-WASM. Query.Farm's compatibility table lists WASM as not available, while its own browser examples run on Haybarn-Wasm. The viewer does not depend on it; test it in M3 for the analysis panel.
4. Proposing `a5` as a registered name in the Zarr DGGS convention and in xdggs.
5. Hosting for heavy jobs (OSM walking-network routing over all of Switzerland): GitHub
   runners versus a small dedicated VM.
6. Domain and CDN choice for the public bucket.
7. ~~deck.gl `TileLayer` with a custom A5 `Tileset2D`~~ Resolved: `TilesetClass` works in
   deck.gl 9.4 (Section 10.2).
8. `@geoarrow/deck.gl-geoarrow` 0.4 now ships a `GeoArrowA5Layer` (wrapping deck.gl's
   `A5Layer` for Arrow cell-ID columns). `A5ParquetLayer` could render Arrow tiles through it
   instead of typed arrays; worth benchmarking against the binary `SolidPolygonLayer` path.
9. Upstream: deck.gl's `A5Layer` should move to `a5-js` 1.x (today it pins 0.10, whose IDs
   differ); until then Qualimap forces 1.0 with an npm override.

---

## 14. Implementation status (v0)

Everything in Sections 3–10 is implemented in this repository, with these notes:

- **Verified here:** the full pipeline on synthetic upstreams (pytest: fetch → stage → cells →
  compute → QA → export → publish, nothing-to-do reruns, forced rebuilds, diff, rollback);
  `qm demo` releases read by the JS readers (node tests); the phase-0 page and the web app
  in headless Chrome (all zoom bands, single-indicator Zarr view, binary path, hover, POIs,
  filters, language switch, rankings with fly-to); the analysis SQL against native DuckDB
  versus the JS engine.
- **Not yet run against real services:** real upstream downloads (endpoints and year-specific
  URLs are configured through `${QM_*}` variables), Backblaze B2 (the code path is the same
  fsspec/S3 one, tested against local stores), the DuckDB `a5` extension (the pipeline uses
  `pya5` and treats the extension as an optional accelerator), the swisstopo base map, and
  DuckDB-WASM's Parquet extension download (the panel falls back to the JS engine when it is
  unavailable).
- **Simplifications to revisit:** colour computation runs on the main thread (no worker yet);
  the binary polygon path is opt-in; the res-11 universe for quintile breaks is read whole.

---

## References

- A5: <https://a5geo.org>, indexing API <https://a5geo.org/docs/api-reference/indexing>,
  DuckDB quickstart <https://a5geo.org/docs/quickstart/duckdb>; Python `pya5` (PyPI);
  TypeScript `a5-js`
- DuckDB `a5` extension: <https://query.farm/products/extensions/a5/>,
  <https://duckdb.org/community_extensions/extensions/a5>
- deck.gl A5Layer: <https://deck.gl/docs/api-reference/geo-layers/a5-layer>
- deck.gl-raster `ZarrLayer`: <https://developmentseed.org/deck.gl-raster/api/deck-gl-zarr/>,
  <https://developmentseed.org/deck.gl-raster/blog/initial-geozarr/>
- GeoArrow deck.gl layers: <https://github.com/geoarrow/deck.gl-layers>; Lonboard architecture:
  <https://developmentseed.org/lonboard/latest/how-it-works/>
- GeoZarr conventions and Zarr DGGS convention: <https://github.com/zarr-developers/geozarr-spec>,
  <https://github.com/zarr-conventions/dggs>
- xdggs conventions: <https://xdggs.readthedocs.io/en/stable/user_guide/conventions.html>
- DuckDB-WASM extensions: <https://duckdb.org/docs/preview/clients/wasm/extensions>
- Indicator methodology carried over from the VivaMap Technical Specification & Methodology
  Manual (noise, air, public transport, taxation, POI access, green space).
