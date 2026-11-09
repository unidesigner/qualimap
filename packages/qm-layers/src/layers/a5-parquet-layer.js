/**
 * A5ParquetLayer: personal score from all `q_*` columns, streamed per tile from
 * `cells_r{res}.parquet` (SPEC §10.3). Weight changes recolour cached tiles; no refetch.
 */
import { colorize, RAMPS } from '../ramps.js';
import { ParquetCells } from '../readers/parquet.js';
import { personalScores } from '../score.js';
import { A5TileLayer } from './a5-tile-layer.js';

export class A5ParquetLayer extends A5TileLayer {
  static layerName = 'A5ParquetLayer';

  static defaultProps = {
    ...A5TileLayer.defaultProps,
    /** res → URL of cells_r{res}.parquet */
    getUrl: { type: 'function', value: (res) => `parquet/cells_r${res}.parquet`, compare: false },
    /** indicator ids that feed the score, in weight order */
    indicators: { type: 'array', value: [], compare: true },
    /** weights 0–1, same order as `indicators` */
    weights: { type: 'array', value: [], compare: true },
    /** optional per-cell mask: (content, index) => boolean (e.g. DuckDB filter results) */
    mask: { type: 'function', value: null, compare: false },
    maskKey: '',
    ramp: { type: 'array', value: RAMPS.viridis, compare: true },
    breaks: { type: 'array', value: null, compare: true },
    extraColumns: { type: 'array', value: ['pop', 'commune_bfs', 'flags'], compare: true },
  };

  initializeState(context) {
    super.initializeState(context);
    this.setState({ readers: new Map() });
  }

  updateState(params) {
    super.updateState(params);
    const { props, oldProps, changeFlags } = params;
    if (!changeFlags.propsChanged) return;
    const cols = (p) => `${p.indicators?.join(',')}|${p.extraColumns?.join(',')}`;
    // Tiles hold only the columns requested at load time; refetch when the set changes.
    if (cols(props) !== cols(oldProps)) this.state.tileset?.reloadAll();
  }

  reader(res) {
    const readers = this.state.readers;
    if (!readers.has(res)) readers.set(res, ParquetCells.open(this.props.getUrl(res)));
    return readers.get(res);
  }

  async loadTile(index) {
    const reader = await this.reader(index.res);
    const cols = [...this.props.indicators.map((i) => `q_${i}`), ...this.props.extraColumns];
    const tile = await reader.readTile(index.cell, cols);
    if (tile) tile.res = index.res;
    return tile;
  }

  colorKey() {
    const { weights, ramp, breaks, maskKey, indicators } = this.props;
    return `${indicators.join(',')}|${weights.join(',')}|${ramp.length}:${ramp[0]}|${breaks ?? ''}|${maskKey}`;
  }

  tileColors(content) {
    const { indicators, weights, ramp, breaks, mask } = this.props;
    const q = indicators.map((i) => content.columns[`q_${i}`]).filter(Boolean);
    const w = indicators.map((_, j) => weights[j] ?? 0);
    content.scores = personalScores(q, w, content.scores);
    if (mask) {
      for (let i = 0; i < content.length; i++) if (!mask(content, i)) content.scores[i] = NaN;
    }
    return colorize(content.scores, { ramp, breaks, out: content._colors });
  }

  cellObject(content, index) {
    return { ...super.cellObject(content, index), score: content.scores?.[index] };
  }
}
