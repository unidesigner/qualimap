# Qualimap

Open quality-of-life maps on the **A5** pentagonal discrete global grid, starting with
Switzerland.

A scheduled Python pipeline fetches open government and OpenStreetMap data, converts it to
A5 cell indicators (noise, air quality, public transport, taxation, access to education,
dining, health and culture, green space), and publishes immutable releases to an
S3-compatible object store (Backblaze B2) as **GeoParquet** and **GeoZarr**. Two deck.gl
viewers read those files directly from the bucket and let each user weight the dimensions in
the browser. There is no application server.

- [`docs/SPEC.md`](docs/SPEC.md): architecture, A5 grid design, storage layout, pipeline,
  output formats, CLI and viewers; Section 14 lists what is verified and what is not yet
  run against real services
- [`catalog/indicators.yaml`](catalog/indicators.yaml): data sources, indicators and
  dimensions; the file to extend with new sources (schema in `catalog/schema/`)

## Repository

| Path | What |
| --- | --- |
| `pipeline/` | Python package `qualimap` and the `qm` CLI |
| `packages/qm-layers/` | deck.gl layers (A5 tiles from GeoParquet and Zarr, raster GeoZarr, GeoArrow communes/POIs), readers, analysis queries |
| `viewer/` | Phase 0: a single HTML page, no build step |
| `apps/web/` | Phase 1: Next.js static export with a DuckDB-WASM analysis panel |
| `.github/workflows/` | `ci.yml` (lint, tests, builds) and `pipeline.yml` (daily `qm run --due`) |
| `docker/` | Pipeline image |

## Quick start

Requires Python 3.12 and Node 22.

```bash
pip install -e "pipeline[dev]"     # add ,osm,zonal,shell,analysis or use [all] for every extra
qm catalog validate
pytest pipeline                    # end-to-end run on synthetic upstreams

qm demo                            # synthetic release in the production layout → .qm/pub
npm install
npm test                           # qm-layers tests (use the demo release when present)
npm run viewer                     # phase-0 page on http://localhost:5173 serving .qm/pub
npm run web:dev                    # Next.js app on http://localhost:3000
                                   #   open /?data=http://localhost:5173/ to use the demo release
```

Without S3 settings the pipeline reads and writes local stores under `.qm/`. For Backblaze
B2, copy `.env.example` to `.env` and fill in the bucket, key and upstream URL settings;
`qm doctor` checks the setup and `qm cors apply` sets the public bucket's CORS rules.

## CLI

```text
qm status                            qm run --due | --all [--until export] [--force]
qm sources list [--due|--stale]      qm fetch / stage / compute / export / publish
qm sources show <id>                 qm reingest <id…> [--from-raw]
qm indicators list | show <id>       qm releases list | diff <a> <b> | rollback <v>
qm catalog validate | graph          qm qa run [--compare-neighbourhoods]
qm cells build                       qm inspect cell <id|lon,lat> | commune <bfs|name>
qm demo                              qm gc · qm doctor · qm cors apply · qm shell
```

`qm run` exits 0 when it published, 2 when QA blocked the release, 3 when nothing was due,
1 on failure. `qm shell` opens IPython with `catalog`, `store`, `duck` (DuckDB) and `a5`
loaded. Global options: `--catalog <yaml>` (repeatable overlays), `--env <file>`, `--json`,
`--dry-run`. See Section 8 of the spec for the full reference.

## Viewers

Both viewers build the same deck.gl layer stack from `@qualimap/qm-layers`:

- **Phase 0** (`viewer/index.html`): pinned ES modules from esm.sh through an import map.
  Host it anywhere and pass `?data=<public bucket URL>`.
- **Phase 1** (`apps/web`): `npm run web:build` writes a static site to `apps/web/out`.
  Build-time settings: `QM_DATA` (public bucket URL), `QM_BASE_PATH`, `QM_MAP_STYLE`,
  `QM_DUCKDB_EXTENSIONS` (self-hosted DuckDB-WASM extension mirror). The analysis panel loads
  DuckDB-WASM only when opened and falls back to a JavaScript engine with identical results.

## Data attribution

Outputs are derived from data by swisstopo, FSO, FOEN, FOCA, ARE, SFOE, FTA,
opentransportdata.swiss and © OpenStreetMap contributors (ODbL). See each source's `license`
entry in the catalog.
