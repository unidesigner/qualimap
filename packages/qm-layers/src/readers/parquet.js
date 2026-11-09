/**
 * Tile reader for `cells_r{res}.parquet` (SPEC §6.1): one row group per tile cell at res − 5.
 * Opens the footer once with HTTP range requests, then reads one row group per tile.
 */
import { cellToParent, u64ToHex } from 'a5-js';
import { asyncBufferFromUrl, cachedAsyncBuffer, parquetMetadataAsync, parquetRead } from 'hyparquet';
import { compressors } from 'hyparquet-compressors';

import { tileResolution } from '../zoom.js';

/**
 * @typedef {object} CellTile
 * @property {BigUint64Array} cellIds
 * @property {Record<string, Uint8Array|Float32Array|Uint16Array|Uint32Array>} columns
 * @property {number} length
 */

const TYPED = { q_: Uint8Array, raw_: Float32Array, pop: Float32Array, commune_bfs: Uint16Array, flags: Uint32Array };

function typedFor(name) {
  for (const [prefix, T] of Object.entries(TYPED)) if (name === prefix || name.startsWith(prefix)) return T;
  return Float64Array;
}

export class ParquetCells {
  /** @param {{file: any, metadata: any, res: number, tileRes: number, groups: Map<string, {start: number, end: number}>}} s */
  constructor(s) {
    Object.assign(this, s);
  }

  /**
   * @param {string|{byteLength: number, slice: Function}} source URL or AsyncBuffer
   * @param {{requestInit?: RequestInit}} [opts]
   */
  static async open(source, opts = {}) {
    const raw = typeof source === 'string' ? await asyncBufferFromUrl({ url: source, requestInit: opts.requestInit }) : source;
    const file = cachedAsyncBuffer(raw);
    const metadata = await parquetMetadataAsync(file);
    const kv = Object.fromEntries((metadata.key_value_metadata ?? []).map((k) => [k.key, k.value]));
    const info = kv.qualimap ? JSON.parse(kv.qualimap) : {};
    const res = info.a5_resolution;
    const tileRes = info.tile_resolution ?? tileResolution(res);
    const groups = new Map();
    let offset = 0;
    for (const rg of metadata.row_groups) {
      const n = Number(rg.num_rows);
      const stats = rg.columns[0].meta_data.statistics ?? {};
      const min = BigInt.asUintN(64, BigInt(stats.min_value ?? stats.min));
      const tile = tileRes === res ? min : cellToParent(min, tileRes);
      groups.set(u64ToHex(tile), { start: offset, end: offset + n });
      offset += n;
    }
    const columns = metadata.schema.slice(1).map((s) => s.name);
    return new ParquetCells({ file, metadata, res, tileRes, groups, columns, info });
  }

  hasTile(hex) {
    return this.groups.has(hex);
  }

  /** @returns {Promise<CellTile|null>} */
  async readTile(hex, columns) {
    const g = this.groups.get(hex);
    if (!g) return null;
    const wanted = ['cell_id', ...columns.filter((c) => c !== 'cell_id' && this.columns.includes(c))];
    const parts = Object.fromEntries(wanted.map((c) => [c, []]));
    await parquetRead({
      file: this.file,
      metadata: this.metadata,
      compressors,
      columns: wanted,
      rowStart: g.start,
      rowEnd: g.end,
      onChunk: ({ columnName, columnData, rowStart }) => parts[columnName].push([rowStart, columnData]),
    });
    const n = g.end - g.start;
    const out = { length: n, columns: {} };
    for (const c of wanted) {
      const T = c === 'cell_id' ? BigUint64Array : typedFor(c);
      const arr = new T(n);
      for (const [rowStart, data] of parts[c]) {
        const off = rowStart - g.start;
        for (let i = 0; i < data.length; i++) {
          const v = data[i];
          arr[off + i] = c === 'cell_id' ? BigInt.asUintN(64, BigInt(v)) : v ?? (T === Uint8Array ? 255 : NaN);
        }
      }
      if (c === 'cell_id') out.cellIds = arr;
      else out.columns[c] = arr;
    }
    return out;
  }
}
