/**
 * Qualimap viewer, phase 0 (SPEC §10.7): MapLibre basemap + deck.gl layers from
 * @qualimap/qm-layers, reading a release straight from the public bucket.
 *
 * URL parameters: ?data=<public bucket URL> (default: same origin), ?release=<version>,
 * ?style=<MapLibre style URL>, ?interleaved=0 (deck.gl canvas above the map), ?debug (exposes
 * globalThis.__qm). View state lives in the fragment (#w=…&z=…&ll=…&ind=…&m=single).
 */
import { MapboxOverlay } from '@deck.gl/mapbox';
import { A5Layer } from '@deck.gl/geo-layers';
import maplibregl from 'maplibre-gl';

import * as qm from '@qualimap/qm-layers';

const params = new URLSearchParams(location.search);
const DATA = params.get('data') ?? globalThis.QUALIMAP_DATA ?? new URL('./', location.href).href;
const STYLE = params.get('style') ?? 'https://vectortiles.geo.admin.ch/styles/ch.swisstopo.lightbasemap.vt/style.json';
const BLANK_STYLE = { version: 8, sources: {}, layers: [{ id: 'bg', type: 'background', paint: { 'background-color': '#eef1f5' } }] };
const RASTER_DOMAINS = {
  foen_sonbase_road_day: [30, 80], foen_sonbase_road_night: [25, 70],
  foen_no2: [0, 45], foen_pm10: [0, 30], foen_pm25: [0, 20],
};
const I18N = {
  en: { weighted: 'All dimensions, weighted', single: 'One indicator', weights: 'Your weights', reset: 'Reset weights',
        overlays: 'Overlays', pois: 'Points of interest', classed: 'Quintile classes', fast: 'Fast polygon path',
        hover: 'Hover a cell for details.', score: 'Your score', release: 'Release', nodata: 'no data' },
  de: { weighted: 'Alle Dimensionen, gewichtet', single: 'Ein Indikator', weights: 'Ihre Gewichtung', reset: 'Zurücksetzen',
        overlays: 'Überlagerungen', pois: 'Points of Interest', classed: 'Quintilklassen', fast: 'Schneller Polygonpfad',
        hover: 'Zelle überfahren für Details.', score: 'Ihre Bewertung', release: 'Release', nodata: 'keine Daten' },
  fr: { weighted: 'Toutes les dimensions, pondérées', single: 'Un indicateur', weights: 'Vos pondérations', reset: 'Réinitialiser',
        overlays: 'Superpositions', pois: "Points d'intérêt", classed: 'Classes par quintiles', fast: 'Rendu rapide',
        hover: 'Survolez une cellule pour les détails.', score: 'Votre score', release: 'Version', nodata: 'pas de données' },
  it: { weighted: 'Tutte le dimensioni, ponderate', single: 'Un indicatore', weights: 'I vostri pesi', reset: 'Reimposta',
        overlays: 'Sovrapposizioni', pois: "Punti d'interesse", classed: 'Classi per quintili', fast: 'Rendering veloce',
        hover: 'Passa su una cella per i dettagli.', score: 'Il vostro punteggio', release: 'Versione', nodata: 'nessun dato' },
};

const $ = (s) => document.querySelector(s);
const state = {
  release: null, catalog: null, indicators: [], weights: [], mode: 'weighted', indicator: null,
  lang: 'en', communes: null, pois: null, rasters: {}, classed: false, breaks: null, binary: false,
  hover: null, r11: null,
};
let map;
let overlay;
let breaksTimer;

function t(key) {
  return (I18N[state.lang] ?? I18N.en)[key] ?? I18N.en[key] ?? key;
}

function applyI18n() {
  document.documentElement.lang = state.lang;
  for (const el of document.querySelectorAll('[data-i18n]')) el.textContent = t(el.dataset.i18n);
  buildControls();
}

function syncHash() {
  const c = map.getCenter();
  history.replaceState(null, '', qm.writeHash({
    weights: qm.encodeWeights(state.weights), zoom: map.getZoom(), center: [c.lng, c.lat],
    indicator: state.indicator, mode: state.mode === 'single' ? 'single' : null,
  }));
}

function buildControls() {
  const box = $('#sliders');
  box.innerHTML = '';
  state.indicators.forEach((ind, j) => {
    const row = document.createElement('div');
    row.className = 'slider';
    const id = `w-${ind.id}`;
    const dim = state.catalog.dimensions.find((d) => d.id === ind.dimension);
    row.innerHTML = `<label for="${id}">${qm.label(dim?.label ?? ind.title, state.lang)}</label>
      <output for="${id}">${Math.round(state.weights[j] * 100)}%</output>
      <input id="${id}" type="range" min="0" max="100" step="5" value="${Math.round(state.weights[j] * 100)}" />`;
    row.querySelector('input').addEventListener('input', (e) => {
      state.weights[j] = Number(e.target.value) / 100;
      row.querySelector('output').textContent = `${e.target.value}%`;
      onWeightsChanged();
    });
    box.appendChild(row);
  });
  const sel = $('#indicator');
  sel.innerHTML = state.catalog.indicators
    .filter((i) => state.catalog.present_indicators.includes(i.id))
    .map((i) => `<option value="${i.id}">${qm.label(i.title, state.lang)}</option>`).join('');
  sel.value = state.indicator;
  const rt = $('#raster-toggles');
  rt.innerHTML = '';
  for (const [sid] of Object.entries(state.catalog.files.rasters ?? {})) {
    const src = state.catalog.sources.find((s) => s.id === sid);
    const l = document.createElement('label');
    l.innerHTML = `<input type="checkbox" data-raster="${sid}" ${state.rasters[sid]?.on ? 'checked' : ''}/> ${src?.title ?? sid}`;
    l.querySelector('input').addEventListener('change', (e) => toggleRaster(sid, e.target.checked));
    rt.appendChild(l);
  }
}

function onWeightsChanged() {
  syncHash();
  render();
  if (state.classed) {
    clearTimeout(breaksTimer);
    breaksTimer = setTimeout(updateBreaks, 100);
  }
}

async function updateBreaks() {
  if (!state.classed) {
    state.breaks = null;
    return render();
  }
  if (!state.r11) {
    const reader = await qm.ParquetCells.open(state.release.url(state.catalog.files.cells['11']));
    const cols = [...state.indicators.map((i) => `q_${i.id}`), 'pop'];
    const tiles = await Promise.all([...reader.groups.keys()].map((h) => reader.readTile(h, cols)));
    const merged = {};
    for (const c of cols) {
      const n = tiles.reduce((s, x) => s + x.length, 0);
      const arr = new (c === 'pop' ? Float32Array : Uint8Array)(n);
      let o = 0;
      for (const x of tiles) { arr.set(x.columns[c], o); o += x.length; }
      merged[c] = arr;
    }
    state.r11 = merged;
  }
  const scores = qm.personalScores(state.indicators.map((i) => state.r11[`q_${i.id}`]), state.weights);
  state.breaks = qm.quantileBreaks(scores, 5, state.r11.pop);
  render();
}

async function toggleRaster(sid, on) {
  const meta = state.catalog.files.rasters[sid];
  state.rasters[sid] = state.rasters[sid] ?? { on: false, node: null };
  state.rasters[sid].on = on;
  if (on && !state.rasters[sid].node) state.rasters[sid].node = await qm.openRaster(state.release.url(meta.path));
  render();
}

function showDetails(info) {
  const tip = $('#tooltip');
  const o = info?.object;
  if (!o) {
    tip.hidden = true;
    return;
  }
  let score = o.score;
  let rows = [];
  if (o.cell_id !== undefined) {
    const q = state.indicators.map((i) => o[`q_${i.id}`] ?? 255);
    rows = qm.breakdown(q, state.weights, state.indicators.map((i) => i.id));
  } else if (info.layer?.id === 'communes') {
    const idx = info.index;
    const tbl = state.communes;
    const q = state.indicators.map((i) => tbl.getChild(`q_${i.id}`)?.get(idx) ?? 255);
    rows = qm.breakdown(q, state.weights, state.indicators.map((i) => i.id));
    const s = qm.personalScores(q.map((v) => Uint8Array.of(v)), state.weights);
    score = s[0];
    o.name = tbl.getChild('name')?.get(idx);
  }
  const title = o.name ?? (o.hex ? `A5 ${o.hex} (r${o.resolution})` : 'POI');
  tip.innerHTML = `<strong>${title}</strong><br/>${t('score')}: ${Number.isFinite(score) ? score.toFixed(1) : t('nodata')}`;
  tip.style.left = `${info.x}px`;
  tip.style.top = `${info.y}px`;
  tip.hidden = false;
  const names = Object.fromEntries(state.indicators.map((i) => [i.id, qm.label(i.title, state.lang)]));
  $('#details').innerHTML = `<table>${rows.map((r) => `<tr><td>${names[r.id]}</td><td>${r.score === null ? '–' : r.score.toFixed(1)}</td></tr>`).join('')}</table>`;
}

function render() {
  if (!overlay) return;
  const cat = state.catalog;
  const zoom = map.getZoom();
  const level = qm.levelForZoom(zoom, cat.zoom_table);
  const ids = state.indicators.map((i) => i.id);
  const resolutionForZoom = (z) => qm.resolutionForZoom(z, cat.zoom_table);
  const common = {
    regionBounds: cat.bounds, resolutionForZoom, renderMode: state.binary ? 'binary' : 'a5',
    onHover: (info) => { state.hover = info.object ?? null; showDetails(info); renderSelection(); },
  };
  const layers = [];
  for (const [sid, r] of Object.entries(state.rasters)) {
    if (r.on && r.node) layers.push(qm.rasterLayer({ id: `raster-${sid}`, node: r.node, domain: RASTER_DOMAINS[sid] ?? [0, 100] }));
  }
  if (state.communes) {
    layers.push(qm.communesLayer({
      id: 'communes', table: state.communes, indicators: ids, weights: state.weights,
      breaks: state.breaks, visible: 'geography' in level,
    }));
  }
  if (state.mode === 'weighted') {
    layers.push(new qm.A5ParquetLayer({
      id: 'a5-parquet', ...common, indicators: ids, weights: state.weights, breaks: state.breaks,
      getUrl: (res) => state.release.url(cat.files.cells[String(res)]),
    }));
  } else {
    layers.push(new qm.A5ZarrLayer({ id: 'a5-zarr', ...common, url: state.release.url(cat.files.zarr), indicator: state.indicator }));
  }
  if (state.pois && $('#pois').checked) layers.push(qm.poisLayer({ table: state.pois, visible: zoom >= 13 }));
  if (state.hover?.cell_id !== undefined) {
    layers.push(new A5Layer({
      id: 'selection', data: [state.hover], getPentagon: (d) => d.cell_id, filled: false, stroked: true,
      getLineColor: [255, 255, 255, 255], lineWidthMinPixels: 2,
    }));
  }
  overlay.setProps({ layers });
}

function renderSelection() {
  render();
}

async function main() {
  state.release = await qm.loadRelease(DATA, { version: params.get('release') ?? undefined });
  const cat = (state.catalog = state.release.catalog);
  state.indicators = qm.scoredIndicators(cat);
  const h = qm.parseHash(location.hash);
  const defaults = qm.defaultWeights(cat);
  state.weights = h.weights ? qm.decodeWeights(h.weights, state.indicators.length) : state.indicators.map((i) => defaults[i.id]);
  state.indicator = h.indicator ?? state.indicators[0]?.id;
  state.mode = h.mode === 'single' ? 'single' : 'weighted';
  state.lang = navigator.language?.slice(0, 2) in I18N ? navigator.language.slice(0, 2) : 'en';
  $('#lang').value = state.lang;
  $('#release').textContent = `${t('release')} ${state.release.version}`;
  $('#legend .bar').style.background = qm.rampCss();
  $('#attribution').textContent = [...new Set(cat.sources.map((s) => s.license?.attribution).filter(Boolean))].join(' · ');
  const [w, s, e, n] = cat.bounds;
  map = new maplibregl.Map({
    container: 'map', style: STYLE,
    center: h.center ?? [(w + e) / 2, (s + n) / 2], zoom: h.zoom ?? 12, hash: false,
  });
  let styleOk = false;
  map.once('style.load', () => { styleOk = true; });
  map.on('error', (ev) => {
    if (!styleOk && /style/i.test(String(ev?.error?.message ?? ''))) map.setStyle(BLANK_STYLE);
  });
  setTimeout(() => { if (!styleOk) map.setStyle(BLANK_STYLE); }, 5000);
  overlay = new MapboxOverlay({ interleaved: params.get('interleaved') !== '0', layers: [] });
  map.addControl(overlay);
  if (params.has('debug')) globalThis.__qm = { map, overlay, state, qm }; // console access for debugging
  map.on('moveend', () => { syncHash(); render(); });
  $('#lang').addEventListener('change', (ev) => { state.lang = ev.target.value; applyI18n(); render(); });
  for (const r of document.querySelectorAll('input[name=mode]')) {
    r.checked = r.value === state.mode;
    r.addEventListener('change', (ev) => {
      state.mode = ev.target.value;
      $('#indicator').hidden = state.mode !== 'single';
      syncHash();
      render();
    });
  }
  $('#indicator').hidden = state.mode !== 'single';
  $('#indicator').addEventListener('change', (ev) => { state.indicator = ev.target.value; syncHash(); render(); });
  $('#reset').addEventListener('click', () => { state.weights = state.indicators.map((i) => defaults[i.id]); buildControls(); onWeightsChanged(); });
  $('#pois').addEventListener('change', render);
  $('#classed').addEventListener('change', (ev) => { state.classed = ev.target.checked; updateBreaks(); });
  $('#binary').addEventListener('change', (ev) => { state.binary = ev.target.checked; render(); });
  applyI18n();
  const [communes, pois] = await Promise.all([
    cat.files.communes_arrow ? qm.loadArrow(state.release.url(cat.files.communes_arrow)).catch(() => null) : null,
    cat.files.pois_arrow ? qm.loadArrow(state.release.url(cat.files.pois_arrow)).catch(() => null) : null,
  ]);
  state.communes = communes;
  state.pois = pois;
  map.on('load', render);
  render();
  document.body.dataset.ready = 'true';
}

main().catch((err) => {
  console.error(err);
  $('#release').textContent = `Failed to load release: ${err.message}`;
  document.body.dataset.error = err.message;
});
