/**
 * Analysis engine for the panel (SPEC §10.8): DuckDB-WASM in a worker, reading the release's
 * Parquet files over HTTP range requests; falls back to plain JS over the same files when
 * DuckDB-WASM cannot start (no WebAssembly, blocked worker, missing bundles).
 *
 * Both engines expose:
 *   rankings(indicators, weights, limit) → [{commune_bfs, name, canton, pop, score}]
 *   distribution(indicators, weights, filters, topShare) → {bins, matched, total, matchedTotal, topThreshold}
 */
import {
  distributionCpu, distributionSql, ParquetCells, rankingsCpu, rankingsSql, shapeDistribution,
} from '@qualimap/qm-layers';

import { BASE_PATH } from './config';

/** Cell resolution used for statistics: the finest of 13 or below that the release has. */
export function analysisResolution(catalog) {
  const res = Object.keys(catalog.files.cells).map(Number).filter((r) => r <= 13);
  return res.length ? Math.max(...res) : Math.min(...Object.keys(catalog.files.cells).map(Number));
}

async function duckdbEngine(release) {
  let stage = 'import';
  try {
    return await duckdbEngineStages(release, (s) => { stage = s; });
  } catch (err) {
    throw new Error(`DuckDB-WASM failed at ${stage}: ${err?.message ?? err}`);
  }
}

async function duckdbEngineStages(release, step) {
  const duckdb = await import('@duckdb/duckdb-wasm');
  step('bundle');
  const base = new URL(`${BASE_PATH}/duckdb/`, window.location.origin).href;
  const bundle = await duckdb.selectBundle({
    mvp: { mainModule: `${base}duckdb-mvp.wasm`, mainWorker: `${base}duckdb-browser-mvp.worker.js` },
    eh: { mainModule: `${base}duckdb-eh.wasm`, mainWorker: `${base}duckdb-browser-eh.worker.js` },
  });
  step(`instantiate (${bundle.mainModule.split('/').pop()})`);
  const worker = new Worker(bundle.mainWorker);
  const db = new duckdb.AsyncDuckDB(new duckdb.VoidLogger(), worker);
  await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
  step('connect');
  const conn0 = await db.connect();
  await conn0.query('SELECT 42');
  await conn0.close();
  // Parquet support is an extension that DuckDB-WASM autoloads, by default from
  // extensions.duckdb.org/<version>/<wasm_mvp|wasm_eh>/parquet.duckdb_extension.wasm.
  // QM_DUCKDB_EXTENSIONS points it at a self-hosted mirror of that layout instead.
  const extRepo = process.env.NEXT_PUBLIC_QM_DUCKDB_EXTENSIONS;
  step('register files');
  const cat = release.catalog;
  const res = analysisResolution(cat);
  await db.registerFileURL('communes.parquet', release.url(cat.files.communes), duckdb.DuckDBDataProtocol.HTTP, false);
  await db.registerFileURL('cells.parquet', release.url(cat.files.cells[String(res)]), duckdb.DuckDBDataProtocol.HTTP, false);
  const conn = await db.connect();
  if (extRepo) await conn.query(`SET custom_extension_repository = '${extRepo.replace(/'/g, "''")}'`);
  const query = async (sql) => (await conn.query(sql)).toArray().map((r) => r.toJSON());
  // Fail early (and fall back) if Parquet over HTTP does not work in this environment.
  step('read communes.parquet');
  await query("SELECT count(*) AS n FROM 'communes.parquet'");
  return {
    kind: 'duckdb',
    resolution: res,
    rankings: async (indicators, weights, limit) => (await query(rankingsSql("'communes.parquet'", indicators, weights, limit)))
      .map((r) => ({ ...r, pop: Number(r.pop), score: Number(r.score), commune_bfs: Number(r.commune_bfs) })),
    distribution: async (indicators, weights, filters, topShare) =>
      shapeDistribution(await query(distributionSql("'cells.parquet'", indicators, weights, filters, topShare))),
    close: async () => {
      await conn.close();
      await db.terminate();
    },
  };
}

function concatColumns(tiles, names) {
  const out = {};
  const n = tiles.reduce((s, t) => s + t.length, 0);
  for (const c of names) {
    const first = tiles.find((t) => t.columns[c]);
    if (!first) continue;
    const arr = new first.columns[c].constructor(n);
    let o = 0;
    for (const t of tiles) {
      if (t.columns[c]) arr.set(t.columns[c], o);
      o += t.length;
    }
    out[c] = arr;
  }
  return out;
}

async function cpuEngine(release, communes) {
  const cat = release.catalog;
  const res = analysisResolution(cat);
  const reader = await ParquetCells.open(release.url(cat.files.cells[String(res)]));
  const cache = new Map();
  async function cellColumns(names) {
    const missing = names.filter((c) => !cache.has(c));
    if (missing.length) {
      const tiles = await Promise.all([...reader.groups.keys()].map((h) => reader.readTile(h, missing)));
      for (const [k, v] of Object.entries(concatColumns(tiles.filter(Boolean), missing))) cache.set(k, v);
    }
    return Object.fromEntries(names.filter((c) => cache.has(c)).map((c) => [c, cache.get(c)]));
  }
  const communeCols = (indicators) => {
    const col = (name) => communes.getChild(name)?.toArray() ?? [];
    const cols = { commune_bfs: col('commune_bfs'), name: col('name'), canton: col('canton'), pop: col('pop') };
    for (const id of indicators) cols[`q_${id}`] = col(`q_${id}`);
    return cols;
  };
  return {
    kind: 'cpu',
    resolution: res,
    rankings: async (indicators, weights, limit) => (communes ? rankingsCpu(communeCols(indicators), indicators, weights, limit) : []),
    distribution: async (indicators, weights, filters, topShare) => {
      const names = ['pop', ...indicators.map((i) => `q_${i}`), ...(filters ?? []).map((f) => `raw_${f.indicator}`)];
      return distributionCpu(await cellColumns(names), indicators, weights, filters, topShare);
    },
    close: async () => {},
  };
}

/**
 * Start the best available engine. `prefer: 'cpu'` skips DuckDB (e.g. ?engine=cpu).
 * @returns {Promise<{kind: 'duckdb'|'cpu', resolution: number, error?: string, rankings: Function, distribution: Function, close: Function}>}
 */
export async function startEngine(release, communes, { prefer } = {}) {
  if (prefer !== 'cpu') {
    try {
      return await duckdbEngine(release);
    } catch (err) {
      console.warn('DuckDB-WASM unavailable, using the JavaScript engine:', err);
      const engine = await cpuEngine(release, communes);
      return { ...engine, error: String(err?.message ?? err) };
    }
  }
  return cpuEngine(release, communes);
}
