'use client';

import { label, rampCss, t } from '@qualimap/qm-layers';

function WeightSlider({ id, text, value, onChange }) {
  const pct = Math.round(value * 100);
  return (
    <div className="slider">
      <label htmlFor={id}>{text}</label>
      <output htmlFor={id}>{pct}%</output>
      <input
        id={id}
        type="range"
        min={0}
        max={100}
        step={5}
        value={pct}
        aria-valuetext={`${pct}%`}
        onChange={(e) => onChange(Number(e.target.value) / 100)}
      />
    </div>
  );
}

function FilterEditor({ lang, filters, filterable, onChange }) {
  const update = (i, patch) => onChange(filters.map((f, j) => (j === i ? { ...f, ...patch } : f)));
  return (
    <fieldset>
      <legend>{t(lang, 'filters')}</legend>
      {filters.map((f, i) => {
        const ind = filterable.find((x) => x.id === f.indicator);
        return (
          <div className="filter" key={i}>
            <select aria-label={t(lang, 'filters')} value={f.indicator} onChange={(e) => update(i, { indicator: e.target.value })}>
              {filterable.map((x) => <option key={x.id} value={x.id}>{label(x.title, lang)}</option>)}
            </select>
            <button type="button" onClick={() => onChange(filters.filter((_, j) => j !== i))} aria-label={t(lang, 'remove')}>×</button>
            <div className="row">
              <select aria-label="operator" value={f.op} onChange={(e) => update(i, { op: e.target.value })}>
                <option value="lte">{t(lang, 'atMost')}</option>
                <option value="gte">{t(lang, 'atLeast')}</option>
              </select>
              <input
                type="number"
                aria-label="value"
                value={Number.isFinite(f.value) ? f.value : ''}
                step="any"
                onChange={(e) => update(i, { value: e.target.value === '' ? Number.NaN : Number(e.target.value) })}
              />
              <span className="muted">{ind?.unit ?? ''}</span>
            </div>
          </div>
        );
      })}
      <button
        type="button"
        onClick={() => onChange([...filters, { indicator: filterable[0]?.id, op: 'lte', value: filterable[0]?.normalize?.good ?? 0 }])}
        disabled={!filterable.length}
      >
        {t(lang, 'addFilter')}
      </button>
    </fieldset>
  );
}

export default function ControlPanel(p) {
  const { lang, release, catalog, indicators, weights } = p;
  const dims = Object.fromEntries((catalog.dimensions ?? []).map((d) => [d.id, d]));
  const present = new Set(catalog.present_indicators ?? []);
  const allIndicators = catalog.indicators.filter((i) => present.has(i.id));
  const filterable = allIndicators.filter((i) => i.column_raw);
  const attribution = [...new Set(catalog.sources.map((s) => s.license?.attribution).filter(Boolean))].join(' · ');
  return (
    <aside className="panel left" aria-label="Controls">
      <header>
        <h1>Qualimap</h1>
        <select aria-label={t(lang, 'language')} value={lang} onChange={(e) => p.setLang(e.target.value)}>
          {(catalog.languages ?? ['de', 'fr', 'it', 'en']).map((l) => <option key={l} value={l}>{l.toUpperCase()}</option>)}
        </select>
      </header>
      <p className="muted">{t(lang, 'release')} {release.version}</p>

      <fieldset>
        <legend>{t(lang, 'map')}</legend>
        <label><input type="radio" name="mode" value="weighted" checked={p.mode === 'weighted'} onChange={() => p.setMode('weighted')} /> {t(lang, 'weighted')}</label>
        <label><input type="radio" name="mode" value="single" checked={p.mode === 'single'} onChange={() => p.setMode('single')} /> {t(lang, 'single')}</label>
        {p.mode === 'single' && (
          <select aria-label="Indicator" value={p.indicator} onChange={(e) => p.setIndicator(e.target.value)}>
            {allIndicators.map((i) => <option key={i.id} value={i.id}>{label(i.title, lang)}</option>)}
          </select>
        )}
      </fieldset>

      <fieldset>
        <legend>{t(lang, 'weights')}</legend>
        {indicators.map((ind, j) => (
          <WeightSlider
            key={ind.id}
            id={`w-${ind.id}`}
            text={label(dims[ind.dimension]?.label ?? ind.title, lang)}
            value={weights[j] ?? 0}
            onChange={(v) => p.setWeight(j, v)}
          />
        ))}
        <button type="button" onClick={p.resetWeights}>{t(lang, 'reset')}</button>
      </fieldset>

      {p.mode === 'weighted' && <FilterEditor lang={lang} filters={p.filters} filterable={filterable} onChange={p.setFilters} />}

      <fieldset>
        <legend>{t(lang, 'overlays')}</legend>
        {Object.keys(catalog.files.rasters ?? {}).map((sid) => (
          <label key={sid}>
            <input type="checkbox" checked={!!p.rasters[sid]} onChange={(e) => p.toggleRaster(sid, e.target.checked)} />{' '}
            {catalog.sources.find((s) => s.id === sid)?.title ?? sid}
          </label>
        ))}
        <label><input type="checkbox" checked={p.showPois} onChange={(e) => p.setShowPois(e.target.checked)} /> {t(lang, 'pois')}</label>
        <label><input type="checkbox" checked={p.classed} onChange={(e) => p.setClassed(e.target.checked)} /> {t(lang, 'classed')}</label>
        <label><input type="checkbox" checked={p.binary} onChange={(e) => p.setBinary(e.target.checked)} /> {t(lang, 'fast')}</label>
      </fieldset>

      <div className="legend" aria-hidden="true">
        <div className="bar" style={{ background: rampCss() }} />
        <div className="ticks"><span>0</span><span>5</span><span>10</span></div>
      </div>

      <section aria-live="polite">
        {p.details ? (
          <>
            <h3>{p.details.title}</h3>
            <p>{t(lang, 'score')}: {Number.isFinite(p.details.score) ? p.details.score.toFixed(1) : t(lang, 'nodata')}</p>
            <table className="data">
              <tbody>
                {p.details.rows.map((r) => (
                  <tr key={r.id}>
                    <td>{label(catalog.indicators.find((i) => i.id === r.id)?.title, lang)}</td>
                    <td className="num">{r.score === null ? '–' : r.score.toFixed(1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ) : (
          <p className="muted">{t(lang, 'hover')}</p>
        )}
      </section>
      <footer className="attribution muted">{attribution}</footer>
    </aside>
  );
}
