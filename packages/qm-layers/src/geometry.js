/** Geometry helpers for GeoArrow tables (no deck.gl dependency, usable in Node tests). */
/**
 * [west, south, east, north] of row `index` of a GeoArrow (multi)polygon column, read from the
 * list offsets without materialising the nested geometry.
 */
export function geometryBounds(table, index, column = 'geometry') {
  const vec = table.getChild(column);
  let local = index;
  let data = null;
  for (const d of vec.data) {
    if (local < d.length) { data = d; break; }
    local -= d.length;
  }
  if (!data) return null;
  // Descend through list levels to the coordinate range covered by this row.
  let lo = local + data.offset;
  let hi = lo + 1;
  let d = data;
  // List levels have valueOffsets; the coordinate level is a FixedSizeList (listSize 2).
  while (d.valueOffsets && d.type.listSize === undefined) {
    const offs = d.valueOffsets;
    lo = offs[lo];
    hi = offs[hi];
    d = d.children[0];
    lo += d.offset;
    hi += d.offset;
  }
  const xy = d.children[0].values; // FixedSizeList<2> of float64
  let w = Infinity;
  let s = Infinity;
  let e = -Infinity;
  let n = -Infinity;
  for (let i = lo; i < hi; i++) {
    const x = xy[2 * i];
    const y = xy[2 * i + 1];
    if (x < w) w = x;
    if (x > e) e = x;
    if (y < s) s = y;
    if (y > n) n = y;
  }
  return Number.isFinite(w) ? [w, s, e, n] : null;
}
