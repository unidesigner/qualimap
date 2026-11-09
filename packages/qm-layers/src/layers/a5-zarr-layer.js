/**
 * A5ZarrLayer: one indicator's uint8 score array, streamed per tile from the A5 GeoZarr store
 * (SPEC §10.4). Switching the indicator swaps the array and refetches.
 */
import { colorize, RAMPS } from '../ramps.js';
import { ZarrCells } from '../readers/zarr.js';
import { NODATA, SCALE } from '../score.js';
import { A5TileLayer } from './a5-tile-layer.js';

export class A5ZarrLayer extends A5TileLayer {
  static layerName = 'A5ZarrLayer';

  static defaultProps = {
    ...A5TileLayer.defaultProps,
    /** URL of qualimap.zarr */
    url: { type: 'string', value: '' },
    indicator: { type: 'string', value: '' },
    ramp: { type: 'array', value: RAMPS.viridis, compare: true },
  };

  initializeState(context) {
    super.initializeState(context);
    this.setState({ readers: new Map() });
  }

  updateState(params) {
    super.updateState(params);
    if (params.changeFlags.propsChanged && params.oldProps.indicator !== params.props.indicator) {
      this.reloadTiles();
    }
  }

  reader(res) {
    const readers = this.state.readers;
    if (!readers.has(res)) readers.set(res, ZarrCells.open(this.props.url, res));
    return readers.get(res);
  }

  async loadTile(index) {
    const reader = await this.reader(index.res);
    const name = `q_${this.props.indicator}`;
    const tile = await reader.readTile(index.cell, [name]);
    if (tile) {
      tile.res = index.res;
      tile.indicator = this.props.indicator;
    }
    return tile;
  }

  colorKey() {
    return `${this.props.indicator}|${this.props.ramp[0]}`;
  }

  tileColors(content) {
    const q = content.columns[`q_${content.indicator}`];
    const scores = new Float32Array(content.length);
    for (let i = 0; i < content.length; i++) scores[i] = q[i] === NODATA ? NaN : q[i] / SCALE;
    content.scores = scores;
    return colorize(scores, { ramp: this.props.ramp });
  }

  cellObject(content, index) {
    return { ...super.cellObject(content, index), score: content.scores?.[index] };
  }
}
