/**
 * UI strings for the viewers in the four catalog languages (SPEC §10.7, §10.8). Indicator and
 * dimension labels come from catalog.json; these are only the interface strings.
 */

export const LANGUAGES = ['de', 'fr', 'it', 'en'];

export const STRINGS = {
  en: {
    weighted: 'All dimensions, weighted', single: 'One indicator', map: 'Map', weights: 'Your weights',
    reset: 'Reset weights', overlays: 'Overlays', pois: 'Points of interest', classed: 'Quintile classes',
    fast: 'Fast polygon path', hover: 'Hover a cell for details.', score: 'Your score', release: 'Release',
    nodata: 'no data', loading: 'Loading release…', failed: 'Failed to load release',
    filters: 'Filters', addFilter: 'Add filter', remove: 'Remove', atMost: 'at most', atLeast: 'at least',
    analysis: 'Analysis', rankings: 'Best communes for your weights', commune: 'Commune', population: 'Population',
    histogram: 'Population by score', matching: 'Population in cells matching the filters',
    engine: 'Engine', engineDuck: 'DuckDB-WASM', engineCpu: 'JavaScript (DuckDB unavailable)',
    topShare: 'Top {p}% for your weights', computing: 'Computing…', canton: 'Canton', language: 'Language',
  },
  de: {
    weighted: 'Alle Dimensionen, gewichtet', single: 'Ein Indikator', map: 'Karte', weights: 'Ihre Gewichtung',
    reset: 'Zurücksetzen', overlays: 'Überlagerungen', pois: 'Points of Interest', classed: 'Quintilklassen',
    fast: 'Schneller Polygonpfad', hover: 'Zelle überfahren für Details.', score: 'Ihre Bewertung', release: 'Release',
    nodata: 'keine Daten', loading: 'Release wird geladen…', failed: 'Release konnte nicht geladen werden',
    filters: 'Filter', addFilter: 'Filter hinzufügen', remove: 'Entfernen', atMost: 'höchstens', atLeast: 'mindestens',
    analysis: 'Analyse', rankings: 'Beste Gemeinden für Ihre Gewichtung', commune: 'Gemeinde', population: 'Bevölkerung',
    histogram: 'Bevölkerung nach Bewertung', matching: 'Bevölkerung in Zellen, die den Filtern entsprechen',
    engine: 'Engine', engineDuck: 'DuckDB-WASM', engineCpu: 'JavaScript (DuckDB nicht verfügbar)',
    topShare: 'Top {p}% für Ihre Gewichtung', computing: 'Berechnung…', canton: 'Kanton', language: 'Sprache',
  },
  fr: {
    weighted: 'Toutes les dimensions, pondérées', single: 'Un indicateur', map: 'Carte', weights: 'Vos pondérations',
    reset: 'Réinitialiser', overlays: 'Superpositions', pois: "Points d'intérêt", classed: 'Classes par quintiles',
    fast: 'Rendu rapide', hover: 'Survolez une cellule pour les détails.', score: 'Votre score', release: 'Version',
    nodata: 'pas de données', loading: 'Chargement de la version…', failed: 'Échec du chargement',
    filters: 'Filtres', addFilter: 'Ajouter un filtre', remove: 'Supprimer', atMost: 'au plus', atLeast: 'au moins',
    analysis: 'Analyse', rankings: 'Meilleures communes selon vos pondérations', commune: 'Commune',
    population: 'Population', histogram: 'Population par score', matching: 'Population dans les cellules filtrées',
    engine: 'Moteur', engineDuck: 'DuckDB-WASM', engineCpu: 'JavaScript (DuckDB indisponible)',
    topShare: 'Top {p} % selon vos pondérations', computing: 'Calcul…', canton: 'Canton', language: 'Langue',
  },
  it: {
    weighted: 'Tutte le dimensioni, ponderate', single: 'Un indicatore', map: 'Mappa', weights: 'I vostri pesi',
    reset: 'Reimposta', overlays: 'Sovrapposizioni', pois: "Punti d'interesse", classed: 'Classi per quintili',
    fast: 'Rendering veloce', hover: 'Passa su una cella per i dettagli.', score: 'Il vostro punteggio',
    release: 'Versione', nodata: 'nessun dato', loading: 'Caricamento…', failed: 'Caricamento non riuscito',
    filters: 'Filtri', addFilter: 'Aggiungi filtro', remove: 'Rimuovi', atMost: 'al massimo', atLeast: 'almeno',
    analysis: 'Analisi', rankings: 'Migliori comuni per i vostri pesi', commune: 'Comune', population: 'Popolazione',
    histogram: 'Popolazione per punteggio', matching: 'Popolazione nelle celle filtrate',
    engine: 'Motore', engineDuck: 'DuckDB-WASM', engineCpu: 'JavaScript (DuckDB non disponibile)',
    topShare: 'Top {p}% per i vostri pesi', computing: 'Calcolo…', canton: 'Cantone', language: 'Lingua',
  },
};

/** Translate `key`, falling back to English, with `{name}` placeholders. */
export function t(lang, key, vars) {
  const s = (STRINGS[lang] ?? STRINGS.en)[key] ?? STRINGS.en[key] ?? key;
  return vars ? s.replace(/\{(\w+)\}/g, (_, k) => String(vars[k] ?? '')) : s;
}

/** First supported language from a list of preferences (e.g. navigator.languages). */
export function pickLanguage(preferences, available = LANGUAGES) {
  for (const p of preferences ?? []) {
    const code = String(p).slice(0, 2).toLowerCase();
    if (available.includes(code)) return code;
  }
  return available.includes('en') ? 'en' : available[0];
}
