/**
 * Release discovery: latest.json → catalog.json (SPEC §4.2, §10.7).
 *
 * @typedef {object} Release
 * @property {string} version
 * @property {string} base     Absolute URL of the release directory (ends with "/").
 * @property {any} catalog     Parsed catalog.json.
 * @property {(path: string) => string} url  Resolve a release-relative path.
 */

/**
 * @param {string} publicBaseUrl  URL of the public bucket (the directory that holds `releases/`).
 * @param {{version?: string, fetch?: typeof fetch}} [opts]
 * @returns {Promise<Release>}
 */
export async function loadRelease(publicBaseUrl, opts = {}) {
  const f = opts.fetch ?? fetch;
  const root = new URL(publicBaseUrl.endsWith('/') ? publicBaseUrl : `${publicBaseUrl}/`, globalThis.location?.href);
  let version = opts.version;
  let baseRel = version ? `releases/${version}/` : null;
  if (!version) {
    const res = await f(new URL('releases/latest.json', root));
    if (!res.ok) throw new Error(`no latest.json at ${root} (${res.status})`);
    const latest = await res.json();
    version = latest.version;
    baseRel = latest.base || `releases/${version}/`;
  }
  const base = new URL(baseRel, root).href;
  const res = await f(new URL('catalog.json', base));
  if (!res.ok) throw new Error(`catalog.json missing for release ${version}`);
  const catalog = await res.json();
  return { version, base, catalog, url: (p) => new URL(p, base).href };
}

/** Indicators that feed the personal score, in catalog order. */
export function scoredIndicators(catalog) {
  const present = new Set(catalog.present_indicators ?? []);
  return (catalog.indicators ?? []).filter((i) => i.scored && present.has(i.id));
}

/** Default weights (0–1) keyed by indicator id, from the dimensions' default_weight. */
export function defaultWeights(catalog) {
  const dims = Object.fromEntries((catalog.dimensions ?? []).map((d) => [d.id, d.default_weight ?? 1]));
  return Object.fromEntries(scoredIndicators(catalog).map((i) => [i.id, dims[i.dimension] ?? 1]));
}

/** Label in the requested language, falling back to English. */
export function label(obj, lang = 'en') {
  if (!obj) return '';
  if (typeof obj === 'string') return obj;
  return obj[lang] ?? obj.en ?? Object.values(obj)[0] ?? '';
}
