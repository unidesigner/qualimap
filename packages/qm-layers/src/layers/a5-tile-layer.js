/**
 * Base deck.gl TileLayer whose tiles are A5 cells (SPEC §10.2).
 *
 * Subclasses implement `loadTile(index, signal)` → `{cellIds, columns, length}` and
 * `tileColors(content)` → RGBA Uint8Array (one colour per cell). Rendering uses deck.gl's
 * `A5Layer` (mode "a5") or a binary `SolidPolygonLayer` with precomputed boundaries
 * (mode "binary", the fast path).
 */
import { TileLayer } from '@deck.gl/geo-layers';
import { A5Layer } from '@deck.gl/geo-layers';
import { SolidPolygonLayer } from '@deck.gl/layers';
import { cellToBoundary, u64ToHex } from 'a5-js';

import { A5Tileset } from '../a5-tileset.js';
import { resolutionForZoom } from '../zoom.js';

/** Flat vertex buffer for many cells (6 vertices per closed pentagon ring). */
export function boundaryBuffers(cellIds) {
  const rings = [];
  let total = 0;
  for (let i = 0; i < cellIds.length; i++) {
    const r = cellToBoundary(cellIds[i], { closedRing: true, segments: 1 });
    rings.push(r);
    total += r.length;
  }
  const positions = new Float64Array(total * 2);
  const startIndices = new Uint32Array(cellIds.length + 1);
  let v = 0;
  for (let i = 0; i < rings.length; i++) {
    startIndices[i] = v;
    for (const [x, y] of rings[i]) {
      positions[2 * v] = x;
      positions[2 * v + 1] = y;
      v++;
    }
  }
  startIndices[rings.length] = v;
  return { positions, startIndices, vertexCount: v };
}

/** Expand per-cell RGBA to per-vertex RGBA for binary polygon attributes. */
export function perVertexColors(cellColors, startIndices) {
  const n = startIndices.length - 1;
  const out = new Uint8Array(startIndices[n] * 4);
  for (let i = 0; i < n; i++) {
    for (let v = startIndices[i]; v < startIndices[i + 1]; v++) {
      out.set(cellColors.subarray(4 * i, 4 * i + 4), 4 * v);
    }
  }
  return out;
}

export class A5TileLayer extends TileLayer {
  static layerName = 'A5TileLayer';

  static defaultProps = {
    ...TileLayer.defaultProps,
    TilesetClass: A5Tileset,
    /** zoom → data resolution (null hides the layer, e.g. commune zooms) */
    resolutionForZoom: { type: 'function', value: (z) => resolutionForZoom(z), compare: false },
    /** [west, south, east, north] of the release; tiles outside are never requested */
    regionBounds: { type: 'array', value: null, compare: true },
    maxTiles: 400,
    renderMode: 'a5', // 'a5' | 'binary'
    minZoom: 0,
    maxZoom: 30,
    refinementStrategy: 'best-available',
    pickable: true,
    opacity: 0.85,
    stroked: false,
    getLineColor: [255, 255, 255, 60],
    lineWidthMinPixels: 0,
  };

  _getTilesetOptions() {
    return {
      ...super._getTilesetOptions(),
      a5Resolution: this.props.resolutionForZoom,
      regionBounds: this.props.regionBounds,
      maxTiles: this.props.maxTiles,
    };
  }

  /**
   * Refetch every tile (cached content stays on screen until replaced). TileLayer only
   * re-requests tiles during its own tileset update, so run one right after marking them.
   */
  reloadTiles() {
    const { tileset } = this.state;
    if (!tileset) return;
    tileset.reloadAll();
    this._updateTileset();
  }

  getTileData(tile) {
    return this.loadTile(tile.index, tile.signal);
  }

  /** @abstract */
  // eslint-disable-next-line no-unused-vars
  async loadTile(index, signal) {
    throw new Error('A5TileLayer.loadTile must be implemented by a subclass');
  }

  /** @abstract */
  // eslint-disable-next-line no-unused-vars
  tileColors(content) {
    throw new Error('A5TileLayer.tileColors must be implemented by a subclass');
  }

  /** Key that changes whenever colours must be recomputed (weights, indicator, ramp...). */
  colorKey() {
    return '';
  }

  renderSubLayers(props) {
    const content = props.data;
    if (!content || !content.length) return null;
    const key = this.colorKey();
    if (content._colorKey !== key) {
      content._colors = this.tileColors(content);
      content._colorKey = key;
    }
    const colors = content._colors;
    const base = {
      id: `${props.id}-cells-${this.props.renderMode}`,
      pickable: this.props.pickable,
      opacity: this.props.opacity,
      updateTriggers: { getFillColor: key },
    };
    if (this.props.renderMode === 'binary') {
      content._geom ??= boundaryBuffers(content.cellIds);
      const g = content._geom;
      if (content._vertexColorKey !== key) {
        content._vertexColors = perVertexColors(colors, g.startIndices);
        content._vertexColorKey = key;
      }
      return new SolidPolygonLayer({
        ...base,
        data: {
          length: content.length,
          startIndices: g.startIndices,
          attributes: {
            getPolygon: { value: g.positions, size: 2 },
            getFillColor: { value: content._vertexColors, size: 4, normalized: true },
          },
        },
        _normalize: false,
        positionFormat: 'XY',
      });
    }
    return new A5Layer({
      ...base,
      data: { length: content.length },
      getPentagon: (_, { index }) => content.cellIds[index],
      getFillColor: (_, { index, target }) => {
        target[0] = colors[4 * index];
        target[1] = colors[4 * index + 1];
        target[2] = colors[4 * index + 2];
        target[3] = colors[4 * index + 3];
        return target;
      },
      stroked: this.props.stroked,
      getLineColor: this.props.getLineColor,
      lineWidthMinPixels: this.props.lineWidthMinPixels,
    });
  }

  getPickingInfo(params) {
    const info = super.getPickingInfo(params);
    const content = info.tile?.content;
    if (content && info.index >= 0 && info.index < content.length) {
      info.object = this.cellObject(content, info.index);
    }
    return info;
  }

  cellObject(content, index) {
    const cell = content.cellIds[index];
    const values = Object.fromEntries(Object.entries(content.columns).map(([k, arr]) => [k, arr[index]]));
    return { cell_id: cell, hex: u64ToHex(cell), resolution: content.res, ...values };
  }
}
