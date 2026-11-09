/**
 * @qualimap/qm-layers — deck.gl layers and data access shared by the static viewer
 * (viewer/index.html) and the Next.js app (apps/web). See SPEC §10.
 */
export { A5Tileset, a5TileIndices, cellBBox, coverBounds, tileBBox } from './a5-tileset.js';
export { A5ParquetLayer } from './layers/a5-parquet-layer.js';
export { A5TileLayer, boundaryBuffers, perVertexColors } from './layers/a5-tile-layer.js';
export { A5ZarrLayer } from './layers/a5-zarr-layer.js';
export { LV95, openRaster, rasterImage, rasterLayer, registerSwissCrs } from './layers/raster.js';
export { communeScores, communesLayer, loadArrow, poisLayer, rgbaData } from './layers/vectors.js';
export { colorize, NODATA_COLOR, RAMPS, rampColor, rampCss } from './ramps.js';
export { ParquetCells } from './readers/parquet.js';
export { ZarrCells } from './readers/zarr.js';
export { defaultWeights, label, loadRelease, scoredIndicators } from './release.js';
export { breakdown, NODATA, personalScores, quantileBreaks, SCALE } from './score.js';
export { DEFAULT_ZOOM_TABLE, levelForZoom, resolutionForZoom, tileResolution } from './zoom.js';
export { activeFilters, cellMask, filterColumns, filterKey, filterSql, passes } from './filters.js';
export { LANGUAGES, pickLanguage, STRINGS, t } from './i18n.js';
export { buildLayerStack, RASTER_DOMAINS } from './stack.js';
export { decodeWeights, encodeWeights, parseHash, writeHash } from './url-state.js';
