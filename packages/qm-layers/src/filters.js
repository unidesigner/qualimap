/**
 * Cell filters on raw indicator values (SPEC §10.8), e.g. "noise ≤ 55 dB(A) and public
 * transport ≥ 0.5 departures/min". A filter is `{indicator, op: 'lte' | 'gte', value}` and
 * reads `raw_<indicator>` in the indicator's own unit. Cells without a raw value fail the filter.
 *
 * The same definition drives the tile mask in `A5ParquetLayer` (cells that fail are drawn as
 * no data) and the SQL used by the analysis panel, so map and statistics agree.
 */

const ID = /^[a-z][a-z0-9_]*$/;

/** Filters that are complete and well-formed. */
export function activeFilters(filters) {
  return (filters ?? []).filter(
    (f) => f && ID.test(f.indicator ?? '') && (f.op === 'lte' || f.op === 'gte') && Number.isFinite(f.value),
  );
}

/** Raw columns a tile must carry to evaluate the filters. */
export function filterColumns(filters) {
  return [...new Set(activeFilters(filters).map((f) => `raw_${f.indicator}`))];
}

/** Stable key that changes whenever the filter result can change. */
export function filterKey(filters) {
  return activeFilters(filters)
    .map((f) => `${f.indicator}${f.op === 'lte' ? '<=' : '>='}${f.value}`)
    .join('&');
}

/** True when row `i` of `columns` passes every filter. */
export function passes(filters, columns, i) {
  for (const f of filters) {
    const col = columns[`raw_${f.indicator}`];
    if (!col) return false;
    const v = col[i];
    if (!Number.isFinite(v)) return false;
    if (f.op === 'lte' ? v > f.value : v < f.value) return false;
  }
  return true;
}

/** `(content, index) => boolean` for A5ParquetLayer's `mask` prop, or null without filters. */
export function cellMask(filters) {
  const active = activeFilters(filters);
  if (!active.length) return null;
  return (content, i) => passes(active, content.columns, i);
}

/** SQL predicate for the same filters (identifiers validated, values numeric). */
export function filterSql(filters) {
  const active = activeFilters(filters);
  if (!active.length) return 'TRUE';
  return active
    .map((f) => `(raw_${f.indicator} IS NOT NULL AND NOT isnan(raw_${f.indicator}) AND raw_${f.indicator} ${f.op === 'lte' ? '<=' : '>='} ${Number(f.value)})`)
    .join(' AND ');
}
