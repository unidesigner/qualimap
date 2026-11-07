# Qualimap

Open quality-of-life maps on the **A5** pentagonal discrete global grid, starting with
Switzerland.

A scheduled Python pipeline fetches open government and OpenStreetMap data, converts it to
A5 cell indicators (noise, air quality, public transport, taxation, access to education,
dining, health and culture, green space), and publishes immutable releases to an
S3-compatible object store (Backblaze B2) as **GeoParquet** and **GeoZarr**. A static web
viewer reads those files directly from the bucket and lets each user weight the dimensions
in the browser.

## Status

Specification phase. Start here:

- [`docs/SPEC.md`](docs/SPEC.md): architecture, A5 grid design, storage layout, pipeline,
  output formats, CLI and viewer roadmap
- [`catalog/indicators.yaml`](catalog/indicators.yaml): data sources, indicators and
  dimensions; the file to extend with new sources
- [`catalog/schema/indicators.schema.json`](catalog/schema/indicators.schema.json): JSON
  Schema for the catalog

## Planned CLI

```text
qm sources list [--due|--stale]      qm compute <indicator…> [--force]
qm sources show <id>                 qm export --version <v>
qm reingest <id…> [--from-raw]       qm publish <v>
qm run --due                         qm shell
```

See section 8 of the spec for the full command reference.

## Data attribution

Outputs are derived from data by swisstopo, FSO, FOEN, FOCA, ARE, SFOE, FTA,
opentransportdata.swiss and © OpenStreetMap contributors (ODbL). See each source's `license`
entry in the catalog.
