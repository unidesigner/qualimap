'use client';

import { MapboxOverlay } from '@deck.gl/mapbox';
import { useEffect, useRef, useState } from 'react';
import { Map, useControl } from 'react-map-gl/maplibre';

import { BLANK_STYLE } from '@/lib/config';

/** deck.gl interleaved into MapLibre's WebGL context, so data can sit below basemap labels. */
function DeckGLOverlay(props) {
  const overlay = useControl(() => new MapboxOverlay(props));
  overlay.setProps(props);
  return null;
}

/**
 * Basemap + deck.gl layers. Falls back to a plain background when the style cannot be loaded
 * (offline, blocked tile host) so the data layers still render.
 */
export default function MapView({ mapRef, initialViewState, styleUrl, layers, onMove, onMoveEnd, onHover }) {
  const [style, setStyle] = useState(styleUrl);
  const loaded = useRef(false);
  useEffect(() => {
    const timer = setTimeout(() => {
      if (!loaded.current) setStyle(BLANK_STYLE);
    }, 5000);
    return () => clearTimeout(timer);
  }, []);
  return (
    <Map
      ref={mapRef}
      initialViewState={initialViewState}
      mapStyle={style}
      style={{ position: 'absolute', inset: 0 }}
      onLoad={() => { loaded.current = true; }}
      onError={(ev) => {
        if (!loaded.current && /style|fetch|load/i.test(String(ev?.error?.message ?? ''))) setStyle(BLANK_STYLE);
      }}
      onMove={onMove}
      onMoveEnd={onMoveEnd}
      attributionControl={{ compact: true }}
    >
      <DeckGLOverlay interleaved layers={layers} onHover={onHover} />
    </Map>
  );
}
