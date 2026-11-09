// Local development server for the phase-0 viewer: `npm run viewer`.
// Serves viewer/ with local node_modules (instead of the esm.sh import map) and a release
// directory as static files with HTTP Range support. Point QM_VIEWER_DATA at a public store,
// e.g. `qm demo` output in .qm/pub.
import { fileURLToPath } from 'node:url';

import { defineConfig } from 'vite';

const repo = fileURLToPath(new URL('..', import.meta.url));

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  publicDir: process.env.QM_VIEWER_DATA ?? `${repo}.qm/pub`,
  server: { fs: { allow: [repo] } },
  optimizeDeps: { exclude: ['@qualimap/qm-layers'] },
});
