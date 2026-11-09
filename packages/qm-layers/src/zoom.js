/** Zoom → data resolution (SPEC §10.5), driven by catalog.json `zoom_table`. */

export const DEFAULT_ZOOM_TABLE = [
  { min_zoom: 0, max_zoom: 8, geography: 'communes' },
  { min_zoom: 8, max_zoom: 10, resolution: 11 },
  { min_zoom: 10, max_zoom: 11, resolution: 12 },
  { min_zoom: 11, max_zoom: 12, resolution: 13 },
  { min_zoom: 12, max_zoom: 13, resolution: 14 },
  { min_zoom: 13, max_zoom: 14, resolution: 15 },
  { min_zoom: 14, max_zoom: 24, resolution: 16 },
];

/** @returns {{geography: 'communes'} | {resolution: number}} */
export function levelForZoom(zoom, table = DEFAULT_ZOOM_TABLE) {
  for (const row of table) {
    if (zoom >= row.min_zoom && zoom < row.max_zoom) {
      return row.geography ? { geography: row.geography } : { resolution: row.resolution };
    }
  }
  const last = table[table.length - 1];
  return last.geography ? { geography: last.geography } : { resolution: last.resolution };
}

/** Data resolution for a zoom, or null when communes are shown. */
export function resolutionForZoom(zoom, table = DEFAULT_ZOOM_TABLE) {
  const l = levelForZoom(zoom, table);
  return 'resolution' in l ? l.resolution : null;
}

/** Tile resolution used for row groups / zarr tile index (one tile ≈ 1,024 data cells). */
export function tileResolution(res) {
  return Math.max(res - 5, 0);
}
