/**
 * A deck.gl Tileset2D whose tiles are A5 cells instead of web-mercator XYZ tiles (SPEC §10.2).
 *
 * For data resolution r, a tile is an A5 cell at r − 5 and holds that cell's ≤ 1,024 descendants
 * at r, i.e. exactly one Parquet row group or Zarr tile-index entry. The tiles for a viewport are
 * the parents of the res-r cells covering it.
 */
import { _Tileset2D as Tileset2D } from '@deck.gl/geo-layers';
import {
  cellToBoundary, cellToChildren, cellToParent, getResolution, hexToU64, isCompactionMarker, lonLatToCell,
  polygonToCells, u64ToHex, uncompact,
} from 'a5-js';

import { tileResolution } from './zoom.js';

/** Exact count of the cells a compacted A5 covering expands to at `res`. */
function uncompactedCount(compacted, res) {
  let n = 0;
  for (const c of compacted) {
    if (isCompactionMarker(c)) continue;
    n += 4 ** (res - getResolution(c));
  }
  return n;
}

/**
 * Cells at `res` overlapping a lon/lat bounding box (uncompacted). Returns null when the covering
 * would exceed `limit` cells.
 */
export function coverBounds([west, south, east, north], res, limit = Infinity) {
  const ring = [[west, south], [east, south], [east, north], [west, north], [west, south]];
  const covering = polygonToCells([ring], res, { containment: 'overlapping' });
  if (!covering.length) return [lonLatToCell([(west + east) / 2, (south + north) / 2], res)];
  if (uncompactedCount(covering, res) > limit) return null;
  return Array.from(uncompact(covering));
}

export function intersectBounds(a, b) {
  if (!b) return a;
  const out = [Math.max(a[0], b[0]), Math.max(a[1], b[1]), Math.min(a[2], b[2]), Math.min(a[3], b[3])];
  return out[0] < out[2] && out[1] < out[3] ? out : null;
}

function toU64(cell) {
  return typeof cell === 'string' ? hexToU64(cell) : cell;
}

function extend(box, ring) {
  for (const [x, y] of ring) {
    if (x < box.west) box.west = x;
    if (x > box.east) box.east = x;
    if (y < box.south) box.south = y;
    if (y > box.north) box.north = y;
  }
  return box;
}

const emptyBox = () => ({ west: Infinity, south: Infinity, east: -Infinity, north: -Infinity });

/** Bounding box of one cell's own pentagon. */
export function cellBBox(cell) {
  return extend(emptyBox(), cellToBoundary(toU64(cell), { closedRing: false, segments: 4 }));
}

/**
 * Bounding box of everything a tile cell holds at deeper resolutions.
 *
 * A5 is not geometrically nested: the descendants of a cell spill well beyond its own pentagon
 * (a res-7 cell's res-12 descendants can reach ~0.4° past it). The extent is taken over the
 * descendants `depth` levels down, padded by the size of one such descendant, which bounds the
 * remaining spill of the deeper levels.
 */
export function tileBBox(cell, depth = 3) {
  const c = toU64(cell);
  const kids = cellToChildren(c, getResolution(c) + depth);
  const box = emptyBox();
  let pad = 0;
  for (const k of kids) {
    const kb = extend(emptyBox(), cellToBoundary(k, { closedRing: false, segments: 1 }));
    pad = Math.max(pad, kb.east - kb.west, kb.north - kb.south);
    box.west = Math.min(box.west, kb.west);
    box.south = Math.min(box.south, kb.south);
    box.east = Math.max(box.east, kb.east);
    box.north = Math.max(box.north, kb.north);
  }
  return { west: box.west - pad, south: box.south - pad, east: box.east + pad, north: box.north + pad };
}

/**
 * Tile indices for a viewport: {x: 0, y: 0, z: tileRes, cell: hex, res: dataRes}.
 * @param {{getBounds: () => number[], zoom: number}} viewport
 * @param {{a5Resolution: (zoom: number) => number|null, regionBounds?: number[]|null, maxTiles?: number}} opts
 */
export function a5TileIndices(viewport, opts) {
  const res = opts.a5Resolution(viewport.zoom);
  if (res === null || res === undefined) return [];
  const tres = tileResolution(res);
  const bounds = intersectBounds(viewport.getBounds(), opts.regionBounds ?? null);
  if (!bounds) return [];
  const max = opts.maxTiles ?? 400;
  // Tiles are the parents of the data cells in view, not the tile-resolution cells overlapping
  // the view: A5 parents do not geometrically contain their children (see tileBBox).
  const cells = coverBounds(bounds, res, max * 1024);
  if (!cells) return [];
  const tiles = new Set();
  for (const c of cells) tiles.add(cellToParent(c, tres));
  if (tiles.size > max) return [];
  return Array.from(tiles)
    .sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))
    .map((c) => ({ x: 0, y: 0, z: tres, cell: u64ToHex(c), res }));
}

export class A5Tileset extends Tileset2D {
  getTileIndices({ viewport }) {
    return a5TileIndices(viewport, this.opts);
  }

  getTileId(index) {
    return `${index.res}:${index.cell}`;
  }

  getTileZoom(index) {
    return index.z;
  }

  getTileMetadata(index) {
    return { bbox: tileBBox(index.cell) };
  }

  getParentIndex(index) {
    if (index.z <= 0) return index;
    const parent = cellToParent(hexToU64(index.cell), index.z - 1);
    return { x: 0, y: 0, z: index.z - 1, cell: u64ToHex(parent), res: index.res - 1 };
  }
}
