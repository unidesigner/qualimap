/**
 * Tile reader for the A5 GeoZarr store (SPEC §6.2): `r{res}/tile_ids` + `r{res}/tile_index`
 * give each tile's row range, then one slice per array.
 */
import { u64ToHex } from 'a5-js';
import * as zarr from 'zarrita';

export class ZarrCells {
  constructor(s) {
    Object.assign(this, s);
  }

  /**
   * @param {string|zarr.Readable} source  URL of `qualimap.zarr` or a zarrita store
   * @param {number} res
   */
  static async open(source, res) {
    const store = typeof source === 'string' ? new zarr.FetchStore(source.replace(/\/$/, '')) : source;
    const root = zarr.root(store);
    const group = await zarr.open(root.resolve(`r${res}`), { kind: 'group' });
    const ids = await zarr.get(await zarr.open(group.resolve('tile_ids'), { kind: 'array' }));
    const idx = await zarr.get(await zarr.open(group.resolve('tile_index'), { kind: 'array' }));
    const tiles = new Map();
    for (let i = 0; i < ids.data.length; i++) {
      const first = Number(idx.data[2 * i]);
      const count = Number(idx.data[2 * i + 1]);
      tiles.set(u64ToHex(BigInt.asUintN(64, ids.data[i])), { start: first, end: first + count });
    }
    return new ZarrCells({ store, root, group, res, tiles, arrays: new Map(), attrs: group.attrs });
  }

  async array(name) {
    if (!this.arrays.has(name)) {
      this.arrays.set(name, zarr.open(this.group.resolve(name), { kind: 'array' }));
    }
    return this.arrays.get(name);
  }

  hasTile(hex) {
    return this.tiles.has(hex);
  }

  /** @returns {Promise<{cellIds: BigUint64Array, columns: Record<string, any>, length: number}|null>} */
  async readTile(hex, variables) {
    const t = this.tiles.get(hex);
    if (!t) return null;
    const sel = [zarr.slice(t.start, t.end)];
    const [ids, ...vals] = await Promise.all([
      this.array('cell_ids').then((a) => zarr.get(a, sel)),
      ...variables.map((v) => this.array(v).then((a) => zarr.get(a, sel))),
    ]);
    const cellIds = ids.data instanceof BigUint64Array ? ids.data : BigUint64Array.from(ids.data, (v) => BigInt.asUintN(64, v));
    const columns = Object.fromEntries(variables.map((v, i) => [v, vals[i].data]));
    return { cellIds, columns, length: t.end - t.start };
  }
}
