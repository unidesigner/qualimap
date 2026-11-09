'use client';

import dynamic from 'next/dynamic';

// deck.gl, MapLibre and the readers need `window`: render the app on the client only. The
// static export still ships a page shell that shows the loading state.
const QualimapApp = dynamic(() => import('./QualimapApp'), {
  ssr: false,
  loading: () => <p className="status muted">Loading…</p>,
});

export default function ClientApp() {
  return <QualimapApp />;
}
