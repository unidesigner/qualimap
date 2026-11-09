// Phase 1 web app (SPEC §10.8): static export, no server runtime. Deploy `out/` to any static
// host (or the public bucket next to `releases/`).
//
//   QM_DATA       URL of the public bucket holding `releases/` (default: same origin as the app)
//   QM_BASE_PATH  path prefix when the app is not served from the domain root (e.g. /app)
//   QM_MAP_STYLE  MapLibre style URL (default: swisstopo light base map)
//   QM_DUCKDB_EXTENSIONS  self-hosted DuckDB-WASM extension repository (default: extensions.duckdb.org)
const basePath = process.env.QM_BASE_PATH ?? '';

/** @type {import('next').NextConfig} */
export default {
  output: 'export',
  basePath,
  trailingSlash: true,
  reactStrictMode: true,
  images: { unoptimized: true },
  transpilePackages: ['@qualimap/qm-layers'],
  env: {
    NEXT_PUBLIC_QM_DATA: process.env.QM_DATA ?? '',
    NEXT_PUBLIC_QM_BASE_PATH: basePath,
    NEXT_PUBLIC_QM_DUCKDB_EXTENSIONS: process.env.QM_DUCKDB_EXTENSIONS ?? '',
    NEXT_PUBLIC_QM_MAP_STYLE:
      process.env.QM_MAP_STYLE ?? 'https://vectortiles.geo.admin.ch/styles/ch.swisstopo.lightbasemap.vt/style.json',
  },
};
