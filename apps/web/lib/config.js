/** Build-time configuration (next.config.mjs), overridable per visit with URL parameters. */

export const BASE_PATH = process.env.NEXT_PUBLIC_QM_BASE_PATH ?? '';

export const BLANK_STYLE = {
  version: 8,
  sources: {},
  layers: [{ id: 'bg', type: 'background', paint: { 'background-color': '#eef1f5' } }],
};

/** ?data=, ?release=, ?style=, ?engine=cpu, ?binary — read once on the client. */
export function runtimeConfig() {
  const params = new URLSearchParams(window.location.search);
  const data = params.get('data') || process.env.NEXT_PUBLIC_QM_DATA || new URL(`${BASE_PATH}/`, window.location.origin).href;
  return {
    data,
    release: params.get('release') ?? undefined,
    style: params.get('style') || process.env.NEXT_PUBLIC_QM_MAP_STYLE,
    engine: params.get('engine') ?? undefined,
    binary: params.has('binary'),
  };
}
