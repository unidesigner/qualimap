/**
 * Colour ramps. The default is viridis (perceptually uniform, colour-blind safe).
 * A ramp is a list of [r, g, b] stops sampled evenly over [0, 10].
 */

export const RAMPS = {
  viridis: [
    [68, 1, 84], [72, 40, 120], [62, 74, 137], [49, 104, 142], [38, 130, 142],
    [31, 158, 137], [53, 183, 121], [109, 205, 89], [180, 222, 44], [253, 231, 37],
  ],
  cividis: [
    [0, 34, 78], [18, 53, 112], [59, 73, 108], [87, 92, 109], [112, 113, 115],
    [138, 134, 120], [165, 156, 116], [194, 179, 105], [224, 204, 86], [254, 232, 56],
  ],
  magma: [
    [0, 0, 4], [28, 16, 68], [79, 18, 123], [129, 37, 129], [181, 54, 122],
    [229, 80, 100], [251, 135, 97], [254, 194, 135], [252, 253, 191], [252, 253, 191],
  ],
};

export const NODATA_COLOR = [160, 160, 160, 60];

/** Interpolated colour for a score in [0, 10]; NaN → no-data colour. */
export function rampColor(score, ramp = RAMPS.viridis, alpha = 210) {
  if (!Number.isFinite(score)) return NODATA_COLOR;
  const t = Math.min(Math.max(score / 10, 0), 1) * (ramp.length - 1);
  const i = Math.min(Math.floor(t), ramp.length - 2);
  const f = t - i;
  const a = ramp[i];
  const b = ramp[i + 1];
  return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, alpha];
}

/** Stepped colour for classed display: index of the first break above the score. */
export function classedColor(score, breaks, ramp = RAMPS.viridis, alpha = 210) {
  if (!Number.isFinite(score)) return NODATA_COLOR;
  let c = 0;
  while (c < breaks.length && score > breaks[c]) c++;
  return rampColor((c / breaks.length) * 10, ramp, alpha);
}

/**
 * RGBA buffer for many scores.
 * @param {Float32Array} scores
 * @param {{ramp?: number[][], alpha?: number, breaks?: number[]|null, out?: Uint8Array}} [opts]
 */
export function colorize(scores, opts = {}) {
  const { ramp = RAMPS.viridis, alpha = 210, breaks = null } = opts;
  const out = opts.out && opts.out.length === scores.length * 4 ? opts.out : new Uint8Array(scores.length * 4);
  for (let i = 0; i < scores.length; i++) {
    const c = breaks ? classedColor(scores[i], breaks, ramp, alpha) : rampColor(scores[i], ramp, alpha);
    out[4 * i] = c[0];
    out[4 * i + 1] = c[1];
    out[4 * i + 2] = c[2];
    out[4 * i + 3] = c[3];
  }
  return out;
}

/** CSS linear-gradient for legends. */
export function rampCss(ramp = RAMPS.viridis) {
  const stops = ramp.map((c, i) => `rgb(${c[0]},${c[1]},${c[2]}) ${((i / (ramp.length - 1)) * 100).toFixed(0)}%`);
  return `linear-gradient(to right, ${stops.join(', ')})`;
}
