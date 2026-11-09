import assert from 'node:assert/strict';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { test } from 'node:test';

import { cellToChildren, cellToLonLat, cellToParent, getResolution, hexToU64, lonLatToCell, u64ToHex } from 'a5-js';
import { asyncBufferFromFile } from 'hyparquet';

import { a5TileIndices, cellBBox, coverBounds, tileBBox } from '../src/a5-tileset.js';
import { colorize, rampColor } from '../src/ramps.js';
import { ParquetCells } from '../src/readers/parquet.js';
import { ZarrCells } from '../src/readers/zarr.js';
import { defaultWeights, scoredIndicators } from '../src/release.js';
import { breakdown, personalScores, quantileBreaks } from '../src/score.js';
import { decodeWeights, encodeWeights, parseHash, writeHash } from '../src/url-state.js';
import { levelForZoom, resolutionForZoom, tileResolution } from '../src/zoom.js';

// A demo release built by `qm demo` (QM_TEST_RELEASE overrides the location).
const PUB = process.env.QM_TEST_PUB ?? '/tmp/qm-pub';
const RELEASE = existsSync(join(PUB, 'releases'))
  ? join(PUB, 'releases', readdirSync(join(PUB, 'releases')).filter((d) => /^\d{4}\.\d{2}\.\d+$/.test(d)).sort().at(-1) ?? '')
  : null;

test('personal score ignores no-data and zero weights', () => {
  const a = Uint8Array.from([250, 0, 255, 125]);
  const b = Uint8Array.from([0, 250, 255, 255]);
  const s = personalScores([a, b], [1, 1]);
  assert.deepEqual(Array.from(s.slice(0, 2)), [5, 5]);
  assert.ok(Number.isNaN(s[2]));
  assert.equal(s[3], 5); // only a has data
  const s2 = personalScores([a, b], [1, 0]);
  assert.equal(s2[1], 0);
  const bd = breakdown([250, 0], [1, 1], ['x', 'y']);
  assert.equal(bd[0].contribution, 5);
});

test('quantile breaks and colours', () => {
  const s = Float32Array.from([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, NaN]);
  const br = quantileBreaks(s, 5);
  assert.equal(br.length, 4);
  assert.deepEqual(rampColor(NaN).slice(0, 3), [160, 160, 160]);
  const rgba = colorize(Float32Array.from([0, 10]));
  assert.equal(rgba.length, 8);
  assert.deepEqual(Array.from(rgba.slice(0, 3)), [68, 1, 84]);
});

test('zoom table and tile resolution', () => {
  assert.deepEqual(levelForZoom(6), { geography: 'communes' });
  assert.equal(resolutionForZoom(8.5), 11);
  assert.equal(resolutionForZoom(15), 16);
  assert.equal(tileResolution(16), 11);
  assert.equal(tileResolution(3), 0);
});

test('url state round trip', () => {
  const w = [1, 0.5, 0, 0.25];
  assert.deepEqual(decodeWeights(encodeWeights(w), 4), w);
  const h = writeHash({ weights: encodeWeights(w), zoom: 12.3, center: [8.54, 47.37], indicator: 'noise' });
  const p = parseHash(h);
  assert.equal(p.indicator, 'noise');
  assert.equal(p.zoom, 12.3);
  assert.deepEqual(p.center, [8.54, 47.37]);
});

test('A5 tiles cover the viewport at res − 5 and are parents of data cells', () => {
  const viewport = { zoom: 14.5, getBounds: () => [8.52, 47.37, 8.55, 47.39] };
  const idx = a5TileIndices(viewport, { a5Resolution: (z) => resolutionForZoom(z), regionBounds: null });
  assert.ok(idx.length > 0 && idx.length < 50);
  assert.ok(idx.every((i) => i.res === 16 && i.z === 11 && getResolution(hexToU64(i.cell)) === 11));
  const inside = lonLatToCell([8.535, 47.38], 16);
  assert.ok(idx.some((i) => i.cell === u64ToHex(cellToParent(inside, 11))));
  const bb = cellBBox(idx[0].cell);
  assert.ok(bb.west < bb.east && bb.south < bb.north);
  assert.equal(a5TileIndices({ zoom: 6, getBounds: () => [5, 45, 11, 48] }, { a5Resolution: resolutionForZoom }).length, 0);
  assert.ok(coverBounds([8.52, 47.37, 8.55, 47.39], 11).length >= 1);
});

test('A5 is not nested: tiles come from data-res cells and tile bboxes contain descendants', () => {
  // Res-7 tile 516fe… lies south of 47.31°N but holds res-12 cells around Zurich (47.38°N).
  const bounds = [8.469, 47.3496, 8.591, 47.4104];
  const own = cellBBox('516fe00000000000');
  assert.ok(own.north < bounds[1], 'precondition: tile pentagon is outside the view');
  const idx = a5TileIndices({ zoom: 10.5, getBounds: () => bounds }, { a5Resolution: () => 12 });
  assert.ok(idx.some((i) => i.cell === '516fe00000000000'));
  const tb = tileBBox('516fe00000000000');
  for (const c of cellToChildren(hexToU64('516fe00000000000'), 12)) {
    const [x, y] = cellToLonLat(c);
    assert.ok(x >= tb.west && x <= tb.east && y >= tb.south && y <= tb.north);
  }
});

const releaseTest = RELEASE && existsSync(join(RELEASE, 'catalog.json')) ? test : test.skip;

releaseTest('catalog helpers on the demo release', () => {
  const catalog = JSON.parse(readFileSync(join(RELEASE, 'catalog.json'), 'utf8'));
  const ids = scoredIndicators(catalog).map((i) => i.id);
  assert.ok(ids.includes('noise') && !ids.includes('rooftop_solar'));
  assert.equal(Object.keys(defaultWeights(catalog)).length, ids.length);
});

releaseTest('ParquetCells reads exactly one tile per row group', async () => {
  const file = await asyncBufferFromFile(join(RELEASE, 'parquet', 'cells_r16.parquet'));
  const pc = await ParquetCells.open(file);
  assert.equal(pc.res, 16);
  assert.equal(pc.tileRes, 11);
  const [hex] = [...pc.groups.keys()].slice(-1);
  const tile = await pc.readTile(hex, ['q_noise', 'q_air', 'pop']);
  assert.ok(tile.length > 0);
  assert.ok(tile.cellIds instanceof BigUint64Array);
  for (const c of tile.cellIds) assert.equal(u64ToHex(cellToParent(c, 11)), hex);
  assert.ok(tile.columns.q_noise instanceof Uint8Array);
  assert.equal(await pc.readTile('0000000000000000', ['q_noise']), null);
});

class FileStore {
  constructor(root) {
    this.root = root;
  }
  async get(key) {
    const p = join(this.root, key);
    return existsSync(p) ? new Uint8Array(readFileSync(p)) : undefined;
  }
}

releaseTest('ZarrCells tile index matches the Parquet tiles', async () => {
  const zc = await ZarrCells.open(new FileStore(join(RELEASE, 'zarr', 'qualimap.zarr')), 16);
  const file = await asyncBufferFromFile(join(RELEASE, 'parquet', 'cells_r16.parquet'));
  const pc = await ParquetCells.open(file);
  assert.deepEqual([...zc.tiles.keys()].sort(), [...pc.groups.keys()].sort());
  const hex = [...zc.tiles.keys()][0];
  const a = await zc.readTile(hex, ['q_noise']);
  const b = await pc.readTile(hex, ['q_noise']);
  assert.deepEqual(Array.from(a.cellIds), Array.from(b.cellIds));
  assert.deepEqual(Array.from(a.columns.q_noise), Array.from(b.columns.q_noise));
});

test('filters: mask, columns, key and SQL agree', async () => {
  const { cellMask, filterColumns, filterKey, filterSql, activeFilters } = await import('../src/filters.js');
  const filters = [
    { indicator: 'noise', op: 'lte', value: 55 },
    { indicator: 'public_transport', op: 'gte', value: 0.5 },
    { indicator: 'bad id; drop', op: 'lte', value: 1 },
    { indicator: 'air', op: 'lte', value: Number.NaN },
  ];
  assert.equal(activeFilters(filters).length, 2);
  assert.deepEqual(filterColumns(filters), ['raw_noise', 'raw_public_transport']);
  assert.equal(filterKey(filters), 'noise<=55&public_transport>=0.5');
  const content = { columns: { raw_noise: Float32Array.of(50, 60, 50, NaN), raw_public_transport: Float32Array.of(1, 1, 0.1, 1) } };
  const mask = cellMask(filters);
  assert.deepEqual([0, 1, 2, 3].map((i) => mask(content, i)), [true, false, false, false]);
  assert.equal(cellMask([]), null);
  assert.match(filterSql(filters), /raw_noise <= 55\) AND .*raw_public_transport >= 0.5\)$/);
  assert.equal(filterSql([]), 'TRUE');
});

test('i18n fallbacks and language choice', async () => {
  const { t, pickLanguage } = await import('../src/i18n.js');
  assert.equal(t('de', 'reset'), 'Zurücksetzen');
  assert.equal(t('xx', 'reset'), 'Reset weights');
  assert.equal(t('fr', 'topShare', { p: 10 }), 'Top 10 % selon vos pondérations');
  assert.equal(pickLanguage(['it-CH', 'de']), 'it');
  assert.equal(pickLanguage(['ja']), 'en');
});
