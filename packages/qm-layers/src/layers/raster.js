/**
 * Raster GeoZarr context overlays with deck.gl-raster's ZarrLayer (SPEC §6.4, §10.1).
 * The store uses the multiscales, geo-proj and spatial conventions in EPSG:2056 (LV95);
 * the LV95 definition is registered with proj4 so no epsg.io request is needed.
 */
import { ZarrLayer } from '@developmentseed/deck.gl-zarr';
import proj4 from 'proj4';
import * as zarr from 'zarrita';

import { rampColor, RAMPS } from '../ramps.js';

export const LV95 =
  '+proj=somerc +lat_0=46.9524055555556 +lon_0=7.43958333333333 +k_0=1 +x_0=2600000 +y_0=1200000 ' +
  '+ellps=bessel +towgs84=674.374,15.056,405.346,0,0,0,0 +units=m +no_defs +type=crs';

let registered = false;
export function registerSwissCrs() {
  if (!registered) {
    proj4.defs('EPSG:2056', LV95);
    registered = true;
  }
}

/** Open a raster GeoZarr group (call once per source, reuse the node). */
export async function openRaster(url) {
  registerSwissCrs();
  const store = new zarr.FetchStore(url.replace(/\/$/, ''));
  return zarr.open(zarr.root(store), { kind: 'group' });
}

/** RGBA ImageData for a float tile; NaN and no-data are transparent. */
export function rasterImage(values, width, height, domain, ramp = RAMPS.magma, nodata = null, alpha = 180) {
  const [lo, hi] = domain;
  const px = new Uint8ClampedArray(width * height * 4);
  for (let i = 0; i < width * height; i++) {
    const v = values[i];
    if (!Number.isFinite(v) || (nodata !== null && v === nodata)) continue;
    const c = rampColor(((v - lo) / (hi - lo)) * 10, ramp, alpha);
    px[4 * i] = c[0];
    px[4 * i + 1] = c[1];
    px[4 * i + 2] = c[2];
    px[4 * i + 3] = c[3];
  }
  return typeof ImageData !== 'undefined' ? new ImageData(px, width, height) : { data: px, width, height };
}

/**
 * @param {{id: string, node: any, domain: [number, number], ramp?: number[][], visible?: boolean,
 *          opacity?: number, nodata?: number|null}} p
 */
export function rasterLayer(p) {
  return new ZarrLayer({
    id: p.id,
    node: p.node,
    selection: {},
    visible: p.visible ?? true,
    opacity: p.opacity ?? 0.7,
    getTileData: async (arr, { sliceSpec, signal }) => {
      const r = await zarr.get(arr, sliceSpec, { opts: { signal } });
      const [height, width] = r.shape;
      return { data: r.data, width, height, byteLength: r.data.byteLength };
    },
    renderTile: (t) => ({
      image: rasterImage(t.data, t.width, t.height, p.domain, p.ramp ?? RAMPS.magma, p.nodata ?? null),
    }),
  });
}
