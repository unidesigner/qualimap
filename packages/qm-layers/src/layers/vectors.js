/**
 * Communes and POIs from GeoArrow IPC files (SPEC §6.3, §6.5, §10.6), rendered with
 * @geoarrow/deck.gl-geoarrow. The pipeline writes interleaved GeoArrow so no conversion is
 * needed in the browser.
 */
import { GeoArrowPolygonLayer, GeoArrowScatterplotLayer } from '@geoarrow/deck.gl-geoarrow';
import * as arrow from 'apache-arrow';

import { colorize, RAMPS } from '../ramps.js';
import { personalScores } from '../score.js';

export async function loadArrow(url, init) {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`failed to load ${url}: ${res.status}`);
  return arrow.tableFromIPC(new Uint8Array(await res.arrayBuffer()));
}

/** arrow.Data<FixedSizeList<Uint8>[4]> from a flat RGBA buffer (GeoArrow colour accessor). */
export function rgbaData(rgba) {
  const n = rgba.length / 4;
  const child = arrow.makeData({ type: new arrow.Uint8(), length: rgba.length, data: rgba });
  return arrow.makeData({
    type: new arrow.FixedSizeList(4, new arrow.Field('rgba', new arrow.Uint8())),
    length: n,
    nullCount: 0,
    child,
  });
}

/** Personal score per commune from the `q_*` columns of the communes table. */
export function communeScores(table, indicators, weights) {
  const q = indicators
    .map((i) => table.getChild(`q_${i}`))
    .filter(Boolean)
    .map((v) => Uint8Array.from(v.toArray()));
  return personalScores(q, indicators.map((_, j) => weights[j] ?? 0));
}

/**
 * @param {{id?: string, table: arrow.Table, indicators: string[], weights: number[], ramp?: number[][],
 *          breaks?: number[]|null, visible?: boolean, pickable?: boolean}} p
 */
export function communesLayer(p) {
  const batch = p.table.batches[0];
  const scores = communeScores(p.table, p.indicators, p.weights);
  const colors = colorize(scores, { ramp: p.ramp ?? RAMPS.viridis, breaks: p.breaks ?? null, alpha: 200 });
  return new GeoArrowPolygonLayer({
    id: p.id ?? 'communes',
    data: batch,
    getPolygon: batch.getChild('geometry').data[0],
    getFillColor: rgbaData(colors),
    getLineColor: [255, 255, 255, 180],
    lineWidthMinPixels: 0.5,
    stroked: true,
    filled: true,
    pickable: p.pickable ?? true,
    visible: p.visible ?? true,
    updateTriggers: { getFillColor: [p.weights.join(','), p.indicators.join(','), String(p.breaks ?? '')] },
    _validate: false,
  });
}

const GROUP_COLORS = {
  education: [31, 120, 180, 230],
  dining: [227, 26, 28, 230],
  health: [51, 160, 44, 230],
  culture: [106, 61, 154, 230],
};

/** POIs coloured by dimension. */
export function poisLayer({ id = 'pois', table, visible = true, radiusMinPixels = 3 }) {
  const batch = table.batches[0];
  const dims = batch.getChild('dimension');
  const n = batch.numRows;
  const rgba = new Uint8Array(n * 4);
  for (let i = 0; i < n; i++) rgba.set(GROUP_COLORS[dims?.get(i)] ?? [90, 90, 90, 200], 4 * i);
  return new GeoArrowScatterplotLayer({
    id,
    data: batch,
    getPosition: batch.getChild('geometry').data[0],
    getFillColor: rgbaData(rgba),
    getLineColor: [255, 255, 255, 255],
    stroked: true,
    lineWidthMinPixels: 1,
    radiusMinPixels,
    getRadius: 6,
    pickable: true,
    visible,
    _validate: false,
  });
}
