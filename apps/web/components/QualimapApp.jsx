'use client';

import {
  breakdown, buildLayerStack, decodeWeights, defaultWeights, encodeWeights, geometryBounds, LANGUAGES, loadArrow,
  loadRelease, openRaster, ParquetCells, parseHash, personalScores, pickLanguage, quantileBreaks, scoredIndicators,
  t, writeHash,
} from '@qualimap/qm-layers';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import AnalysisPanel from './AnalysisPanel';
import ControlPanel from './ControlPanel';
import MapView from './MapView';
import { runtimeConfig } from '@/lib/config';

/** Population-weighted quintile breaks of the personal score over the res-11 cells. */
async function loadR11(release, indicators) {
  const reader = await ParquetCells.open(release.url(release.catalog.files.cells['11']));
  const cols = [...indicators.map((i) => `q_${i.id}`), 'pop'];
  const tiles = (await Promise.all([...reader.groups.keys()].map((h) => reader.readTile(h, cols)))).filter(Boolean);
  const out = {};
  const n = tiles.reduce((s, x) => s + x.length, 0);
  for (const c of cols) {
    const arr = new (c === 'pop' ? Float32Array : Uint8Array)(n);
    let o = 0;
    for (const x of tiles) {
      arr.set(x.columns[c], o);
      o += x.length;
    }
    out[c] = arr;
  }
  return out;
}

export default function QualimapApp() {
  const config = useMemo(() => runtimeConfig(), []);
  const initialHash = useMemo(() => parseHash(window.location.hash), []);
  const mapRef = useRef(null);

  const [release, setRelease] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [lang, setLang] = useState(() => pickLanguage(navigator.languages ?? [navigator.language]));
  const [weights, setWeights] = useState([]);
  const [mode, setMode] = useState(initialHash.mode === 'single' ? 'single' : 'weighted');
  const [indicator, setIndicator] = useState(initialHash.indicator);
  const [filters, setFilters] = useState([]);
  const [classed, setClassed] = useState(false);
  const [breaks, setBreaks] = useState(null);
  const [binary, setBinary] = useState(config.binary);
  const [showPois, setShowPois] = useState(true);
  const [rasters, setRasters] = useState({});
  const [communes, setCommunes] = useState(null);
  const [pois, setPois] = useState(null);
  const [zoom, setZoom] = useState(initialHash.zoom ?? 12);
  const [hover, setHover] = useState(null);
  const [showAnalysis, setShowAnalysis] = useState(false);
  const r11 = useRef(null);

  // Release discovery and the small whole-file tables.
  useEffect(() => {
    let alive = true;
    loadRelease(config.data, { version: config.release })
      .then(async (rel) => {
        if (!alive) return;
        const cat = rel.catalog;
        const inds = scoredIndicators(cat);
        const defaults = defaultWeights(cat);
        setWeights(initialHash.weights ? decodeWeights(initialHash.weights, inds.length) : inds.map((i) => defaults[i.id]));
        setIndicator((cur) => cur ?? inds[0]?.id);
        setLang((cur) => pickLanguage([cur, ...(navigator.languages ?? [])], cat.languages ?? LANGUAGES));
        setRelease(rel);
        const [c, p] = await Promise.all([
          cat.files.communes_arrow ? loadArrow(rel.url(cat.files.communes_arrow)).catch(() => null) : null,
          cat.files.pois_arrow ? loadArrow(rel.url(cat.files.pois_arrow)).catch(() => null) : null,
        ]);
        if (alive) {
          setCommunes(c);
          setPois(p);
        }
      })
      .catch((err) => alive && setLoadError(String(err?.message ?? err)));
    return () => {
      alive = false;
    };
  }, [config, initialHash]);

  const catalog = release?.catalog;
  const indicators = useMemo(() => (catalog ? scoredIndicators(catalog) : []), [catalog]);
  const ids = useMemo(() => indicators.map((i) => i.id), [indicators]);

  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);

  // Quintile classes follow the weights (debounced).
  useEffect(() => {
    if (!classed || !release) {
      setBreaks(null);
      return undefined;
    }
    const timer = setTimeout(async () => {
      r11.current ??= await loadR11(release, indicators);
      const scores = personalScores(indicators.map((i) => r11.current[`q_${i.id}`]), weights);
      setBreaks(quantileBreaks(scores, 5, r11.current.pop));
    }, 100);
    return () => clearTimeout(timer);
  }, [classed, release, indicators, weights]);

  const syncHash = useCallback(() => {
    const map = mapRef.current;
    if (!map || !weights.length) return;
    const c = map.getCenter();
    window.history.replaceState(null, '', writeHash({
      weights: encodeWeights(weights), zoom: map.getZoom(), center: [c.lng, c.lat],
      indicator, mode: mode === 'single' ? 'single' : null,
    }));
  }, [weights, indicator, mode]);
  useEffect(syncHash, [syncHash]);

  const toggleRaster = useCallback(async (sid, on) => {
    if (!on) {
      setRasters((r) => {
        const { [sid]: _drop, ...rest } = r;
        return rest;
      });
      return;
    }
    const node = await openRaster(release.url(catalog.files.rasters[sid].path));
    setRasters((r) => ({ ...r, [sid]: node }));
  }, [release, catalog]);

  const onHover = useCallback((info) => {
    const o = info?.object;
    if (!o || !catalog) {
      setHover(null);
      return;
    }
    if (info.layer?.id === 'communes' || info.layer?.id?.startsWith('communes')) {
      const idx = info.index;
      const q = ids.map((id) => communes?.getChild(`q_${id}`)?.get(idx) ?? 255);
      const score = personalScores(q.map((v) => Uint8Array.of(v)), weights)[0];
      setHover({ x: info.x, y: info.y, title: communes?.getChild('name')?.get(idx) ?? '', score, rows: breakdown(q, weights, ids) });
    } else if (o.cell_id !== undefined) {
      const q = ids.map((id) => o[`q_${id}`] ?? 255);
      const score = o.score ?? personalScores(q.map((v) => Uint8Array.of(v)), weights)[0];
      setHover({ x: info.x, y: info.y, title: `A5 ${o.hex} (r${o.resolution})`, score, rows: breakdown(q, weights, ids), cell: o });
    } else {
      setHover(null);
    }
  }, [catalog, communes, ids, weights]);

  const layers = useMemo(() => {
    if (!release) return [];
    return buildLayerStack({
      release, zoom, indicators: ids, weights, mode, indicator, breaks,
      renderMode: binary ? 'binary' : 'a5', communes, pois, showPois,
      rasters: Object.entries(rasters).map(([id, node]) => ({ id, node })),
      filters, selected: hover?.cell ?? null,
    });
  }, [release, zoom, ids, weights, mode, indicator, breaks, binary, communes, pois, showPois, rasters, filters, hover]);

  const pickCommune = useCallback((bfs) => {
    if (!communes || !mapRef.current) return;
    const col = communes.getChild('commune_bfs');
    for (let i = 0; i < communes.numRows; i++) {
      if (Number(col.get(i)) === Number(bfs)) {
        const b = geometryBounds(communes, i);
        if (b) mapRef.current.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 60, duration: 800, maxZoom: 13 });
        return;
      }
    }
  }, [communes]);

  if (loadError) return <p className="status" role="alert">{t(lang, 'failed')}: {loadError}</p>;
  if (!release || !weights.length) return <p className="status muted">{t(lang, 'loading')}</p>;

  const [w, s, e, n] = catalog.bounds;
  const initialViewState = {
    longitude: initialHash.center?.[0] ?? (w + e) / 2,
    latitude: initialHash.center?.[1] ?? (s + n) / 2,
    zoom: initialHash.zoom ?? 12,
  };

  return (
    <main data-ready="true">
      <MapView
        mapRef={mapRef}
        initialViewState={initialViewState}
        styleUrl={config.style}
        layers={layers}
        onHover={onHover}
        onMove={(ev) => {
          const z = Math.round(ev.viewState.zoom * 4) / 4;
          if (z !== zoom) setZoom(z);
        }}
        onMoveEnd={syncHash}
      />
      <ControlPanel
        lang={lang} setLang={setLang} release={release} catalog={catalog} indicators={indicators}
        weights={weights}
        setWeight={(j, v) => setWeights((ws) => ws.map((x, k) => (k === j ? v : x)))}
        resetWeights={() => {
          const d = defaultWeights(catalog);
          setWeights(indicators.map((i) => d[i.id]));
        }}
        mode={mode} setMode={setMode} indicator={indicator} setIndicator={setIndicator}
        filters={filters} setFilters={setFilters}
        rasters={rasters} toggleRaster={toggleRaster}
        showPois={showPois} setShowPois={setShowPois}
        classed={classed} setClassed={setClassed}
        binary={binary} setBinary={setBinary}
        details={hover}
      />
      {hover && (
        <div className="tooltip" role="tooltip" style={{ left: hover.x, top: hover.y }}>
          <strong>{hover.title}</strong>
          <br />
          {t(lang, 'score')}: {Number.isFinite(hover.score) ? hover.score.toFixed(1) : t(lang, 'nodata')}
        </div>
      )}
      {showAnalysis ? (
        <>
          <AnalysisPanel
            lang={lang} release={release} communes={communes} indicators={indicators} weights={weights}
            filters={mode === 'weighted' ? filters : []} preferEngine={config.engine} onPickCommune={pickCommune}
          />
          <button type="button" className="toggle-analysis" style={{ right: 364 }} onClick={() => setShowAnalysis(false)}>×</button>
        </>
      ) : (
        <button type="button" className="toggle-analysis" onClick={() => setShowAnalysis(true)}>{t(lang, 'analysis')}</button>
      )}
    </main>
  );
}
