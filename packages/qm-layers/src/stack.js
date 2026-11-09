/**
 * The Qualimap layer stack (SPEC §10.1), shared by the phase-0 page and the Next.js app so
 * both draw the same layers in the same order:
 *
 *   raster-<source> → communes → a5-parquet | a5-zarr → pois → selection
 */
import { A5Layer } from '@deck.gl/geo-layers';

import { cellMask, filterColumns, filterKey } from './filters.js';
import { A5ParquetLayer } from './layers/a5-parquet-layer.js';
import { A5ZarrLayer } from './layers/a5-zarr-layer.js';
import { rasterLayer } from './layers/raster.js';
import { communesLayer, poisLayer } from './layers/vectors.js';
import { levelForZoom, resolutionForZoom } from './zoom.js';

/** Display range per raster source (values outside are clamped). */
export const RASTER_DOMAINS = {
  foen_sonbase_road_day: [30, 80],
  foen_sonbase_road_night: [25, 70],
  foen_no2: [0, 45],
  foen_pm10: [0, 30],
  foen_pm25: [0, 20],
};

const BASE_EXTRA = ['pop', 'commune_bfs', 'flags'];

/**
 * @param {object} s
 * @param {{catalog: any, url: (p: string) => string}} s.release
 * @param {number} s.zoom
 * @param {string[]} s.indicators         scored indicator ids, weight order
 * @param {number[]} s.weights
 * @param {'weighted'|'single'} s.mode
 * @param {string} [s.indicator]          for mode 'single'
 * @param {number[]|null} [s.breaks]      quintile breaks or null for a continuous ramp
 * @param {'a5'|'binary'} [s.renderMode]
 * @param {any} [s.communes]              arrow.Table
 * @param {any} [s.pois]                  arrow.Table
 * @param {boolean} [s.showPois]
 * @param {{id: string, node: any}[]} [s.rasters]  open raster nodes to draw
 * @param {{indicator: string, op: 'lte'|'gte', value: number}[]} [s.filters]
 * @param {{cell_id: bigint}|null} [s.selected]
 * @param {(info: any) => void} [s.onHover]
 * @param {(info: any) => void} [s.onClick]
 */
export function buildLayerStack(s) {
  const cat = s.release.catalog;
  const level = levelForZoom(s.zoom, cat.zoom_table);
  const common = {
    regionBounds: cat.bounds,
    resolutionForZoom: (z) => resolutionForZoom(z, cat.zoom_table),
    renderMode: s.renderMode ?? 'a5',
    onHover: s.onHover,
    onClick: s.onClick,
  };
  const layers = [];
  for (const r of s.rasters ?? []) {
    layers.push(rasterLayer({ id: `raster-${r.id}`, node: r.node, domain: RASTER_DOMAINS[r.id] ?? [0, 100] }));
  }
  if (s.communes) {
    layers.push(communesLayer({
      id: 'communes', table: s.communes, indicators: s.indicators, weights: s.weights,
      breaks: s.breaks ?? null, visible: 'geography' in level, pickable: true,
    }));
  }
  if (s.mode === 'single') {
    layers.push(new A5ZarrLayer({ id: 'a5-zarr', ...common, url: s.release.url(cat.files.zarr), indicator: s.indicator }));
  } else {
    const mask = cellMask(s.filters);
    layers.push(new A5ParquetLayer({
      id: 'a5-parquet', ...common, indicators: s.indicators, weights: s.weights, breaks: s.breaks ?? null,
      getUrl: (res) => s.release.url(cat.files.cells[String(res)]),
      extraColumns: [...BASE_EXTRA, ...filterColumns(s.filters)],
      mask, maskKey: filterKey(s.filters),
    }));
  }
  if (s.pois && s.showPois !== false) layers.push(poisLayer({ table: s.pois, visible: s.zoom >= 13 }));
  if (s.selected?.cell_id !== undefined) {
    layers.push(new A5Layer({
      id: 'selection', data: [s.selected], getPentagon: (d) => d.cell_id, filled: false, stroked: true,
      getLineColor: [255, 255, 255, 255], lineWidthMinPixels: 2, pickable: false,
      updateTriggers: { getPentagon: String(s.selected.cell_id) },
    }));
  }
  return layers;
}
