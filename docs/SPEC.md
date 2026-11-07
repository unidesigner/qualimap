# Qualimap — Technical Specification

Status: Draft v0.1 · 2026-11-07
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
                                   viewer: static HTML (v0) → Next.js (v1)
                                   hyparquet / DuckDB-WASM · zarrita · deck.gl A5Layer
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

| Operation | Python (`pya5`) | DuckDB (`a5` ext.) | Browser (`a5-js`) |
| --- | --- | --- | --- |
| Point → cell | `lonlat_to_cell((lon, lat), res)` | `a5_lonlat_to_cell(lon, lat, res)` | `lonLatToCell` |
| Cell → centroid | `cell_to_lonlat` | `a5_cell_to_lonlat` | `cellToLonLat` |
| Cell → polygon | `cell_to_boundary` | `a5_cell_to_boundary` | `cellToBoundary` |
| Parent / children | `cell_to_parent`, `cell_to_children` | `a5_cell_to_parent`, `a5_cell_to_children` | same names |
| Polygon fill | `polygon_to_cells` | — (use Python) | — |
| Neighbourhood | `grid_disk` | — (use Python) | — |
| Compaction | `compact` / `uncompact` | `a5_compact` | — |

Browser names are the camelCase equivalents in `a5-js`; confirm exact signatures against the
pinned version. Argument order is **longitude, latitude** throughout. Early versions of the DuckDB extension
exposed `a5_cell(lat, lon, res)`; the pipeline pins the extension version and only uses
`a5_lonlat_to_cell`.

### 3.4 Cell universe

1. Load the national boundary (swissBOUNDARIES3D `TLM_LANDESGEBIET`, `ICC = 'CH'`), densify
   to 50 m segments in LV95, reproject to WGS84.
2. `a5.polygon_to_cells(boundary, 16)` → ~5.21 M IDs. Partition the work by the res-11
   parents that intersect the boundary.
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
- **Polygons** (communes, noise contours, quality classes): cell centroid in polygon for
  interior cells; exact area shares for cells crossing a boundary.
- **Neighbourhoods** (green share within 300 m): `grid_disk(cell, k)` with
  k = ceil(300 m / mean centroid spacing at res 16), the spacing measured once per build.

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
(`obstore` or `fsspec`/`s3fs` in Python, `rclone` for bulk ops). Configuration comes from the
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
  releases/<version>/zarr/qualimap.zarr/…
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

- **Phase 1:** a GitHub Actions workflow runs `qm run --due` daily at 02:17 UTC in a pinned
  container image (`ghcr.io/unidesigner/qualimap-pipeline:<sha>`). Heavy national jobs
  (OSM routing, raster zonal stats) can target a self-hosted runner via a job label.
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

---

## 6. Output formats

The cell table is published twice from the same data: GeoParquet is the **source of truth**
for tabular and SQL access; GeoZarr is a derived, array-oriented view.

### 6.1 GeoParquet (`parquet/cells_r{11..16}.parquet`)

**Schema (one row per A5 cell):**

| Column | Type | Notes |
| --- | --- | --- |
| `cell_id` | UINT64 | A5 ID, primary key, **sort key** |
| `geometry` | WKB Point | Cell centroid. Polygons are derived client-side from `cell_id`, keeping the file small |
| `bbox` | STRUCT(xmin, ymin, xmax, ymax FLOAT) | GeoParquet 1.1 bbox covering column, for clients without A5 support |
| `commune_bfs` | UINT16 | Majority commune (BFS number) |
| `pop` | FLOAT32 | Residents (dasymetric estimate) |
| `q_<indicator>` | UINT8 | Score 0–250 (= score × 25); 255 = no data |
| `raw_<indicator>` | FLOAT32 | Raw value in the unit declared in the catalog |
| `flags` | UINT32 | Bit flags (stale source, limit exceeded, …), documented in `catalog.json` |

**Physical layout** (this is what makes browser queries cheap):

- Rows sorted by `cell_id`, so A5's contiguous descendant ranges map to contiguous row ranges.
- One row group per res-11 parent, giving ~5,100 row groups of ~1,000 rows at res 16. Row
  group min/max statistics on `cell_id` let a reader skip every row group outside the
  requested ranges. Parquet page index enabled.
- ZSTD level 9; dictionary encoding for `q_*` and `commune_bfs`.
- Footer metadata: `geo` (GeoParquet 1.1, CRS OGC:CRS84), plus `qualimap` key with release
  version, A5 resolution and A5 library version.

Estimated size of `cells_r16.parquet`: 5.2 M rows × ~30 bytes compressed ≈ 150 MB (to be
measured). Coarser files are a few MB each.

**Browser query paths:**

| Path | Library | Use |
| --- | --- | --- |
| Viewport fetch | `hyparquet` (pure JS) | Read footer once, select row groups whose `cell_id` range intersects the viewport's res-11 cells, fetch only needed columns |
| Ad-hoc SQL | DuckDB-WASM + `a5` community extension | `SELECT … FROM 'https://…/cells_r16.parquet' WHERE cell_id BETWEEN ? AND ?`; commune rankings, filters, histograms |

DuckDB-WASM loads community extensions in the browser; whether the `a5` extension's WASM
build works end to end must be confirmed in the first viewer spike. Range filtering works
without it, because IDs and ranges can be computed with `a5-js`.

**Viewport → ID ranges:** compute the res-11 cells covering the viewport with `a5-js`; for
each, the descendant range at the target resolution is
`[cellToChildren(c, r)[0], cellToChildren(c, r).at(-1)]`, computed once and cached. (A
closed-form range function is preferable; track an upstream feature request.)

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
    parent_index uint64 [N11, 2]  # per res-11 parent: [first_row, row_count]
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
- `parent_index` gives the viewer O(1) access to any res-11 parent's rows without reading
  `cell_ids` first: one small read, then one chunk-range read per array.
- Browser access with `zarrita.js` over HTTP range requests.

**When to use which:** the viewer uses Parquet for viewport data and attribute panels, and
Zarr for single-indicator layers at high zoom (one uint8 array, no row decoding). Analysts use
Zarr with xarray/xdggs and Parquet with DuckDB, pandas or QGIS.

### 6.3 Communes

`parquet/communes.parquet`: one row per commune with simplified polygon geometry (WKB,
CRS84), `commune_bfs`, `name`, `canton`, `pop`, and `q_*` / `raw_*` columns, so communes
render without A5 decoding.

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
| `qm export` | Write GeoParquet and GeoZarr for a release | `--version`, `--formats parquet,zarr`, `--res 11-16` |
| `qm publish <version>` | Upload a release and switch `latest.json` | `--no-switch` |
| `qm run` | End-to-end orchestration | `--due` (default), `--all`, `--until stage\|cellify\|compute\|export\|publish` |

### 8.3 Maintenance

| Command | Purpose | Key parameters |
| --- | --- | --- |
| `qm releases list` | Published releases with size and source versions | |
| `qm releases rollback <version>` | Point `latest.json` at an earlier release | |
| `qm releases diff <a> <b>` | Per-indicator change statistics between two releases | `--commune <bfs>` |
| `qm qa run` | Run QA checks without publishing | `--indicator` |
| `qm inspect cell <cell_id\|lon,lat>` | All indicator values and lineage for one cell | `--res` |
| `qm inspect commune <bfs\|name>` | Commune values and ranks | |
| `qm gc` | Apply retention rules to `raw/`, `staged/`, `cells/`, `indicators/` | `--keep-raw 3`, `--dry-run` |
| `qm cors apply` | Apply the CORS rules to the public bucket | |
| `qm doctor` | Check credentials, bucket access, CORS, DuckDB extensions, A5 versions | |
| `qm shell` | Interactive Python prompt (IPython) with `catalog`, `store`, `duck` (DuckDB connection with `spatial` + `a5` loaded) and `a5` preloaded | `--release <version>` |

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

## 10. Viewer

### 10.1 Phase 0: single HTML page (`viewer/index.html`)

No build step; ES modules from a CDN (esm.sh or jsDelivr), pinned versions.

- **Map:** MapLibre GL JS with a swisstopo vector basemap style; deck.gl as a
  `MapboxOverlay` with an `A5Layer` (`getPentagon` reads the cell ID).
- **Data:** read `releases/latest.json`, then `catalog.json` (labels, legends, default
  weights). At low zoom load `cells_r11`–`r13` whole (a few MB); at higher zoom fetch only
  the row groups covering the viewport from `cells_r14`–`r16` with hyparquet.
- **Personal score:** nine weight sliders (0–100%). The client computes
  `Σ wᵢ·qᵢ / Σ wᵢ` over indicators with data, on typed arrays in a Web Worker, and passes
  colours to the layer with `updateTriggers`. Tens of thousands of visible cells recompute in
  a few milliseconds.
- **Interaction:** hover tooltip with the per-indicator breakdown; URL fragment stores
  weights and view (`#w=…&z=…&ll=…`).

Zoom → resolution (MapLibre zoom at ~47° N):

| Zoom | Resolution | File |
| --- | --- | --- |
| < 8 | Communes | `communes.parquet` |
| 8–9.99 | 11 | `cells_r11.parquet` (whole file) |
| 10–10.99 | 12 | `cells_r12.parquet` (whole file) |
| 11–11.99 | 13 | `cells_r13.parquet` (whole file) |
| 12–12.99 | 14 | row groups in viewport |
| 13–13.99 | 15 | row groups in viewport |
| ≥ 14 | 16 | row groups in viewport |

### 10.2 Phase 1: Next.js application (`apps/web`)

- Next.js App Router with **static export** (no server runtime), deployed to any static host.
- TypeScript, React, deck.gl via `@deck.gl/react`, MapLibre via `react-map-gl/maplibre`.
- Data layer package `packages/qm-data`: release discovery, hyparquet and zarrita readers,
  viewport → A5 ranges, caching (in-memory LRU plus Cache API for immutable release files).
- DuckDB-WASM in a worker for the analysis panel: commune rankings for the user's weights,
  histograms, "top x% for your weights", filters (for example "PT class B or better and noise
  < 55 dB").
- i18n: de, fr, it, en from `catalog.json` labels.
- Accessibility: keyboard sliders, colour-blind-safe ramps, text alternative for the map
  selection.
- Performance budget: first meaningful map < 2.5 s on 4G; slider-to-repaint < 50 ms for
  60,000 visible cells. If the CPU path misses that budget, move the weighting into a deck.gl
  shader extension (weights as uniforms, scores as instanced attributes).

---

## 11. Repository layout

```text
qualimap/
  catalog/
    indicators.yaml                 # sources, indicators, dimensions
    schema/indicators.schema.json
    maps/                           # lookup tables used by transforms
    local/                          # optional overlay catalogs (git-ignored or per deployment)
  docs/
    SPEC.md                         # this document
    methodology/                    # per-indicator methodology notes
  pipeline/                         # Python package `qualimap`
    pyproject.toml
    src/qualimap/
      cli.py                        # Typer app (qm)
      catalog.py                    # load, layer, validate, DAG
      store.py                      # S3/B2 access, key layout
      state.py                      # fetch records, run records
      grid.py                       # A5 universe, joins, roll-up
      fetchers/                     # stac, http, estv_api, osm_pbf, gtfs
      transforms/                   # one module per dimension
      export/                       # geoparquet.py, geozarr.py
      qa.py
    tests/
  viewer/index.html                 # phase 0
  apps/web/                         # phase 1 (Next.js)
  packages/qm-data/                 # shared TS data access
  .github/workflows/
    ci.yml                          # lint, tests, catalog validation
    pipeline.yml                    # scheduled qm run --due
  docker/pipeline.Dockerfile
```

Python 3.12; key dependencies: `pya5`, `duckdb` (with `spatial` and community `a5`),
`pyarrow`, `geopandas`, `shapely` 2, `pyproj`, `rasterio`, `exactextract`, `zarr` 3,
`xarray`, `xdggs`, `obstore`, `httpx`, `pystac-client`, `typer`, `rich`, `ipython`,
`pydantic` (catalog models), `jsonschema`.

---

## 12. Roadmap

| Milestone | Deliverable | Done when |
| --- | --- | --- |
| M0 Skeleton | Repo layout, catalog + schema, `qm sources list`, `qm catalog validate`, CI | Catalog validates in CI |
| M1 Grid | A5 universe for CH, GWR + STATPOP joined, roll-up, `qm inspect cell` | Commune population reconciles within 0.1% |
| M2 First indicators | Noise, air, public transport end to end; GeoParquet export to B2 | `cells_r16.parquet` readable via HTTP range in a browser |
| M3 Viewer v0 | `viewer/index.html` with A5Layer and weight sliders | Weighted map works from the public bucket |
| M4 All nine dimensions | Taxation, POI access ×4, green; GeoZarr export | All QA checks pass; release diff tooling |
| M5 Scheduling | GitHub Actions `qm run --due`; alerts; retention | Two consecutive unattended releases |
| M6 Web app | Next.js app with DuckDB-WASM analysis panel and i18n | Feature parity with v0 plus rankings |
| M7 Beyond CH | Region abstraction (`--region`), first non-Swiss pilot region | Catalog supports per-region sources |

---

## 13. Open questions

1. Licence for the code (MIT or Apache-2.0) and for published data (respecting upstream terms,
   e.g. OSM ODbL share-alike for OSM-derived layers, FSO "open use, must provide source").
2. Base resolution 16 versus 15: res 16 gives ~5.2 M cells (~150 MB Parquet); res 15 would
   quarter the size if street-level detail is not needed.
3. Whether the `a5` DuckDB extension's WASM build works in DuckDB-WASM (spike in M3).
4. Proposing `a5` as a registered name in the Zarr DGGS convention and in xdggs.
5. Hosting for heavy jobs (OSM walking-network routing over all of Switzerland): GitHub
   runners versus a small dedicated VM.
6. Domain and CDN choice for the public bucket.

---

## References

- A5: <https://a5geo.org>, Python package `pya5` (PyPI), TypeScript `a5-js`, DuckDB community
  extension `a5` (<https://duckdb.org/community_extensions/extensions/a5>)
- deck.gl A5Layer: <https://deck.gl/docs/api-reference/geo-layers/a5-layer>
- GeoZarr conventions and Zarr DGGS convention: <https://github.com/zarr-developers/geozarr-spec>,
  <https://github.com/zarr-conventions/dggs>
- xdggs conventions: <https://xdggs.readthedocs.io/en/stable/user_guide/conventions.html>
- DuckDB-WASM extensions: <https://duckdb.org/docs/preview/clients/wasm/extensions>
- Indicator methodology carried over from the VivaMap Technical Specification & Methodology
  Manual (noise, air, public transport, taxation, POI access, green space).
