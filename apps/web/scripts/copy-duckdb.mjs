// Self-host the DuckDB-WASM bundles under public/duckdb/ so the analysis panel needs no CDN
// (the static export then carries them; release data stays on the bucket).
import { copyFileSync, mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const dist = dirname(require.resolve('@duckdb/duckdb-wasm/dist/duckdb-mvp.wasm'));
const out = join(dirname(fileURLToPath(import.meta.url)), '..', 'public', 'duckdb');
mkdirSync(out, { recursive: true });
for (const f of ['duckdb-mvp.wasm', 'duckdb-eh.wasm', 'duckdb-browser-mvp.worker.js', 'duckdb-browser-eh.worker.js']) {
  copyFileSync(join(dist, f), join(out, f));
}
console.log(`DuckDB-WASM bundles copied to ${out}`);
