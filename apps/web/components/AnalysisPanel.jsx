'use client';

import { activeFilters, filterKey, t } from '@qualimap/qm-layers';
import { useEffect, useRef, useState } from 'react';

import { startEngine } from '@/lib/analysis-engine';

const fmtPop = (v) => Math.round(v).toLocaleString('de-CH');
const TOP_SHARE = 0.1;

function Histogram({ dist }) {
  const max = Math.max(1, ...dist.bins);
  const w = 300;
  const h = 90;
  const bw = w / 10;
  return (
    <svg className="histogram" viewBox={`0 0 ${w} ${h}`} role="img" aria-label="histogram">
      {dist.bins.map((v, i) => {
        const bh = ((h - 14) * v) / max;
        const mh = ((h - 14) * dist.matched[i]) / max;
        return (
          <g key={i}>
            <rect className="all" x={i * bw + 2} y={h - 14 - bh} width={bw - 4} height={bh} />
            <rect className="matched" x={i * bw + 2} y={h - 14 - mh} width={bw - 4} height={mh} />
            <text x={i * bw + bw / 2} y={h - 2} textAnchor="middle">{i}</text>
          </g>
        );
      })}
    </svg>
  );
}

/**
 * Commune rankings, score distribution and filter statistics for the current weights
 * (SPEC §10.8). The engine starts when the panel first opens.
 */
export default function AnalysisPanel({ lang, release, communes, indicators, weights, filters, preferEngine, onPickCommune }) {
  const [engine, setEngine] = useState(null);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const seq = useRef(0);

  useEffect(() => {
    let alive = true;
    let started = null;
    startEngine(release, communes, { prefer: preferEngine })
      .then((e) => {
        started = e;
        if (alive) setEngine(e);
        else e.close();
      })
      .catch((err) => alive && setError(String(err?.message ?? err)));
    return () => {
      alive = false;
      started?.close();
    };
  }, [release, communes, preferEngine]);

  const ids = indicators.map((i) => i.id);
  const key = `${ids.join(',')}|${weights.join(',')}|${filterKey(filters)}`;
  useEffect(() => {
    if (!engine) return undefined;
    const my = ++seq.current;
    const timer = setTimeout(async () => {
      setBusy(true);
      try {
        const active = activeFilters(filters);
        const [rankings, dist] = await Promise.all([
          engine.rankings(ids, weights, 15),
          engine.distribution(ids, weights, active, TOP_SHARE),
        ]);
        if (my === seq.current) setResult({ rankings, dist });
      } catch (err) {
        if (my === seq.current) setError(String(err?.message ?? err));
      } finally {
        if (my === seq.current) setBusy(false);
      }
    }, 150);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [engine, key]);

  const dist = result?.dist;
  const hasFilters = activeFilters(filters).length > 0;
  return (
    <aside className="panel right" aria-label={t(lang, 'analysis')} data-engine={engine?.kind ?? ''} data-ready={result ? 'true' : 'false'}>
      <header>
        <h2>{t(lang, 'analysis')}</h2>
        <span className="muted">{busy ? t(lang, 'computing') : ''}</span>
      </header>
      <p className="muted">
        {t(lang, 'engine')}: {engine ? (engine.kind === 'duckdb' ? t(lang, 'engineDuck') : t(lang, 'engineCpu')) : t(lang, 'computing')}
        {engine ? ` · r${engine.resolution}` : ''}
      </p>
      {error && <p role="alert">{error}</p>}
      {result && (
        <>
          <h3>{t(lang, 'rankings')}</h3>
          <table className="data">
            <thead>
              <tr><th>#</th><th>{t(lang, 'commune')}</th><th className="num">{t(lang, 'population')}</th><th className="num">{t(lang, 'score')}</th></tr>
            </thead>
            <tbody>
              {result.rankings.map((r, i) => (
                <tr
                  key={r.commune_bfs}
                  className="clickable"
                  tabIndex={0}
                  onClick={() => onPickCommune(r.commune_bfs)}
                  onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && onPickCommune(r.commune_bfs)}
                >
                  <td>{i + 1}</td>
                  <td>{r.name}</td>
                  <td className="num">{fmtPop(r.pop)}</td>
                  <td className="num">{r.score.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h3>{t(lang, 'histogram')}</h3>
          <Histogram dist={dist} />
          {dist.topThreshold !== null && (
            <p>{t(lang, 'topShare', { p: TOP_SHARE * 100 })}: ≥ {dist.topThreshold.toFixed(2)}</p>
          )}
          {hasFilters && (
            <p>
              {t(lang, 'matching')}: {fmtPop(dist.matchedTotal)} / {fmtPop(dist.total)} (
              {dist.total > 0 ? ((100 * dist.matchedTotal) / dist.total).toFixed(1) : '0'} %)
            </p>
          )}
        </>
      )}
    </aside>
  );
}
