/** Shareable view state in the URL fragment: #w=…&z=…&ll=…&ind=… (SPEC §10.7). */

/** Weights 0–1 → two base-36 digits each (0–100 %). */
export function encodeWeights(weights) {
  return weights.map((w) => Math.round(Math.min(Math.max(w, 0), 1) * 100).toString(36).padStart(2, '0')).join('');
}

export function decodeWeights(text, n) {
  const out = [];
  for (let i = 0; i < n; i++) {
    const chunk = text?.slice(2 * i, 2 * i + 2);
    const v = chunk ? parseInt(chunk, 36) : NaN;
    out.push(Number.isFinite(v) ? Math.min(v, 100) / 100 : 1);
  }
  return out;
}

export function parseHash(hash) {
  const p = new URLSearchParams((hash ?? '').replace(/^#/, ''));
  const ll = p.get('ll')?.split(',').map(Number);
  return {
    weights: p.get('w'),
    zoom: p.has('z') ? Number(p.get('z')) : null,
    center: ll && ll.length === 2 && ll.every(Number.isFinite) ? ll : null,
    indicator: p.get('ind'),
    mode: p.get('m'),
  };
}

export function writeHash({ weights, zoom, center, indicator, mode }) {
  const p = new URLSearchParams();
  if (weights) p.set('w', weights);
  if (zoom !== null && zoom !== undefined) p.set('z', zoom.toFixed(2));
  if (center) p.set('ll', `${center[0].toFixed(5)},${center[1].toFixed(5)}`);
  if (indicator) p.set('ind', indicator);
  if (mode) p.set('m', mode);
  return `#${p.toString()}`;
}
