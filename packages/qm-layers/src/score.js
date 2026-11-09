/**
 * Personal score and colour mapping (SPEC §10.3).
 *
 * Scores are stored as uint8 q = round(score × 25), 255 = no data. The personal score of a
 * cell is Σ wᵢ·qᵢ / (25 · Σ wᵢ) over indicators with data; NaN when no weight applies.
 */

export const NODATA = 255;
export const SCALE = 25;

/**
 * @param {Uint8Array[]} qColumns  one array per indicator, equal lengths
 * @param {number[]} weights        one weight (0–1) per column
 * @param {Float32Array} [out]
 * @returns {Float32Array} scores in [0, 10] or NaN
 */
export function personalScores(qColumns, weights, out) {
  const n = qColumns.length ? qColumns[0].length : 0;
  const res = out && out.length === n ? out : new Float32Array(n);
  const k = qColumns.length;
  for (let i = 0; i < n; i++) {
    let num = 0;
    let den = 0;
    for (let j = 0; j < k; j++) {
      const w = weights[j];
      if (w <= 0) continue;
      const q = qColumns[j][i];
      if (q !== NODATA) {
        num += w * q;
        den += w;
      }
    }
    res[i] = den > 0 ? num / (SCALE * den) : NaN;
  }
  return res;
}

/** Contribution of each indicator to one cell's score (for tooltips). */
export function breakdown(qValues, weights, ids) {
  let den = 0;
  for (let j = 0; j < qValues.length; j++) if (qValues[j] !== NODATA && weights[j] > 0) den += weights[j];
  return ids.map((id, j) => {
    const q = qValues[j];
    const score = q === NODATA ? null : q / SCALE;
    const share = q === NODATA || den === 0 ? 0 : weights[j] / den;
    return { id, score, weight: weights[j], contribution: score === null ? 0 : score * share };
  });
}

/** Weighted quantile breaks (e.g. quintiles) of finite scores, optionally weighted by population. */
export function quantileBreaks(scores, k = 5, weights = null) {
  const idx = [];
  for (let i = 0; i < scores.length; i++) if (Number.isFinite(scores[i])) idx.push(i);
  idx.sort((a, b) => scores[a] - scores[b]);
  const w = (i) => (weights ? Math.max(weights[i], 0) : 1);
  const total = idx.reduce((s, i) => s + w(i), 0);
  const breaks = [];
  let acc = 0;
  let next = 1;
  for (const i of idx) {
    acc += w(i);
    while (next < k && acc >= (total * next) / k) {
      breaks.push(scores[i]);
      next++;
    }
  }
  return breaks;
}
