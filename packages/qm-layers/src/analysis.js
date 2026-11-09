/**
 * Analysis panel queries (SPEC §10.8): commune rankings for the user's weights, the population
 * distribution of the personal score, the population matching the cell filters, and the score
 * threshold of the top x %. Each query exists as DuckDB SQL (for DuckDB-WASM over the release's
 * Parquet files) and as a plain-JS equivalent over typed columns (fallback when DuckDB-WASM
 * cannot start). Both use the exact score definition of score.js.
 */
import { activeFilters, filterSql, passes } from './filters.js';
import { NODATA, personalScores, SCALE } from './score.js';

const ID = /^[a-z][a-z0-9_]*$/;

function checkIds(indicators) {
  for (const id of indicators) if (!ID.test(id)) throw new Error(`invalid indicator id: ${id}`);
}

/** SQL expression for the personal score (0–10, NULL without data) of one row. */
export function scoreSql(indicators, weights) {
  checkIds(indicators);
  const terms = indicators
    .map((id, j) => [id, Number(weights[j] ?? 0)])
    .filter(([, w]) => w > 0 && Number.isFinite(w));
  if (!terms.length) return 'CAST(NULL AS DOUBLE)';
  const num = terms.map(([id, w]) => `CASE WHEN q_${id} <> ${NODATA} THEN ${w} * q_${id} ELSE 0 END`).join(' + ');
  const den = terms.map(([id, w]) => `CASE WHEN q_${id} <> ${NODATA} THEN ${w} ELSE 0 END`).join(' + ');
  return `((${num}) / NULLIF(${den}, 0) / ${SCALE})`;
}

/** Top communes by personal score. `source` is a table name or a quoted file path. */
export function rankingsSql(source, indicators, weights, limit = 15) {
  return `SELECT commune_bfs, name, canton, pop, score FROM (
  SELECT commune_bfs, name, canton, pop, ${scoreSql(indicators, weights)} AS score FROM ${source}
) WHERE score IS NOT NULL ORDER BY score DESC, pop DESC LIMIT ${Math.max(1, Math.floor(limit))}`;
}

/**
 * Population per score bin (10 bins of width 1), total and filtered, plus the score threshold
 * of the top `topShare` of the population.
 */
export function distributionSql(source, indicators, weights, filters, topShare = 0.1) {
  const score = scoreSql(indicators, weights);
  const where = filterSql(filters);
  return `WITH s AS (
  SELECT ${score} AS score, COALESCE(pop, 0) AS pop, (${where}) AS ok FROM ${source}
), b AS (
  SELECT LEAST(CAST(FLOOR(score) AS INTEGER), 9) AS bin, SUM(pop) AS pop, SUM(CASE WHEN ok THEN pop ELSE 0 END) AS matched
  FROM s WHERE score IS NOT NULL GROUP BY 1
), c AS (
  SELECT score, SUM(pop) OVER (ORDER BY score DESC ROWS UNBOUNDED PRECEDING) AS cum, SUM(pop) OVER () AS tot
  FROM s WHERE score IS NOT NULL
)
SELECT 'bin' AS kind, bin AS k, pop AS v1, matched AS v2 FROM b
UNION ALL SELECT 'total', NULL, SUM(pop), SUM(CASE WHEN ok THEN pop ELSE 0 END) FROM s
UNION ALL SELECT 'top', NULL, MAX(score) FILTER (WHERE cum >= ${Number(topShare)} * tot), NULL FROM c`;
}

/** Shape the rows of distributionSql into {bins, matched, total, matchedTotal, topThreshold}. */
export function shapeDistribution(rows) {
  const bins = new Array(10).fill(0);
  const matched = new Array(10).fill(0);
  let total = 0;
  let matchedTotal = 0;
  let topThreshold = null;
  for (const r of rows) {
    if (r.kind === 'bin') {
      bins[Number(r.k)] = Number(r.v1 ?? 0);
      matched[Number(r.k)] = Number(r.v2 ?? 0);
    } else if (r.kind === 'total') {
      total = Number(r.v1 ?? 0);
      matchedTotal = Number(r.v2 ?? 0);
    } else if (r.kind === 'top') {
      topThreshold = r.v1 === null || r.v1 === undefined ? null : Number(r.v1);
    }
  }
  return { bins, matched, total, matchedTotal, topThreshold };
}

// ---------------------------------------------------------------------------------------------
// Plain-JS equivalents. `cols` maps column names to typed arrays of equal length.

/** Rankings over a columnar commune table {commune_bfs, name, canton, pop, q_*}. */
export function rankingsCpu(cols, indicators, weights, limit = 15) {
  const scores = personalScores(indicators.map((id) => cols[`q_${id}`]), weights);
  const idx = [];
  for (let i = 0; i < scores.length; i++) if (Number.isFinite(scores[i])) idx.push(i);
  idx.sort((a, b) => scores[b] - scores[a] || (cols.pop[b] ?? 0) - (cols.pop[a] ?? 0));
  return idx.slice(0, limit).map((i) => ({
    index: i, commune_bfs: cols.commune_bfs[i], name: cols.name[i], canton: cols.canton[i], pop: cols.pop[i], score: scores[i],
  }));
}

/** Distribution over a columnar cell table {pop, q_*, raw_*}. */
export function distributionCpu(cols, indicators, weights, filters, topShare = 0.1) {
  const scores = personalScores(indicators.map((id) => cols[`q_${id}`]), weights);
  const active = activeFilters(filters);
  const bins = new Array(10).fill(0);
  const matched = new Array(10).fill(0);
  let total = 0;
  let matchedTotal = 0;
  const order = [];
  for (let i = 0; i < scores.length; i++) {
    const pop = cols.pop?.[i] ?? 0;
    const ok = !active.length || passes(active, cols, i);
    total += pop;
    if (ok) matchedTotal += pop;
    if (!Number.isFinite(scores[i])) continue;
    const b = Math.min(Math.floor(scores[i]), 9);
    bins[b] += pop;
    if (ok) matched[b] += pop;
    order.push(i);
  }
  order.sort((a, b) => scores[b] - scores[a]);
  const scoredTotal = order.reduce((s, i) => s + (cols.pop?.[i] ?? 0), 0);
  let cum = 0;
  let topThreshold = null;
  for (const i of order) {
    cum += cols.pop?.[i] ?? 0;
    if (cum >= topShare * scoredTotal) {
      topThreshold = scores[i];
      break;
    }
  }
  return { bins, matched, total, matchedTotal, topThreshold };
}
