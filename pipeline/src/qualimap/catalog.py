"""Catalog loading, layering, validation and dependency graph (SPEC §7)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import jsonschema
import yaml

ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")
DURATION = re.compile(r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?$")
KNOWN_QA_CHECKS = {
    "range",
    "nodata_share",
    "nodata_share_settlement",
    "count_delta_vs_previous",
    "reconcile_population",
    "compare_reference",
    "golden_values",
    "agreement_with_reference",
}


class CatalogError(ValueError):
    """Raised when the catalog is invalid."""


def parse_duration(text: str) -> timedelta:
    m = DURATION.match(text)
    if not m or not any(m.groups()):
        raise CatalogError(f"invalid ISO 8601 duration: {text!r}")
    y, mo, w, d = (int(g) if g else 0 for g in m.groups())
    return timedelta(days=y * 365 + mo * 30 + w * 7 + d)


def interpolate(value: Any, env: dict[str, str] | None = None) -> Any:
    """Resolve ``${NAME}`` placeholders from the environment. Unresolved names stay as-is."""
    env = os.environ if env is None else env
    if isinstance(value, str):
        return ENV_PATTERN.sub(lambda m: env.get(m.group(1), m.group(0)), value)
    if isinstance(value, list):
        return [interpolate(v, env) for v in value]
    if isinstance(value, dict):
        return {k: interpolate(v, env) for k, v in value.items()}
    return value


def unresolved(value: Any) -> list[str]:
    if isinstance(value, str):
        return ENV_PATTERN.findall(value)
    if isinstance(value, list):
        return [n for v in value for n in unresolved(v)]
    if isinstance(value, dict):
        return [n for v in value.values() for n in unresolved(v)]
    return []


def schema() -> dict:
    """The catalog JSON Schema from the repository's catalog/schema/ (source checkout, or an
    install run from a directory that holds catalog/, as in the Docker image)."""
    starts = [Path(__file__).resolve(), Path.cwd().resolve() / "_"]
    for start in starts:
        for parent in start.parents:
            p = parent / "catalog" / "schema" / "indicators.schema.json"
            if p.exists():
                return json.loads(p.read_text())
    raise FileNotFoundError(
        "catalog/schema/indicators.schema.json not found above the package or the working directory")


@dataclass
class Source:
    raw: dict

    @property
    def id(self) -> str:
        return self.raw["id"]

    @property
    def kind(self) -> str:
        return self.raw["kind"]

    @property
    def fetch(self) -> dict:
        return self.raw.get("fetch", {})

    @property
    def method(self) -> str:
        return self.fetch.get("method", "http")

    @property
    def stage(self) -> dict:
        return self.raw.get("stage", {}) or {}

    @property
    def refresh_every(self) -> timedelta:
        return parse_duration(self.raw["refresh"]["every"])

    @property
    def refresh_check(self) -> str:
        return self.raw["refresh"]["check"]

    @property
    def export_raster_zarr(self) -> bool:
        return bool((self.raw.get("export") or {}).get("raster_zarr"))

    @property
    def attribution(self) -> str:
        return self.raw.get("license", {}).get("attribution", "")


@dataclass
class Indicator:
    raw: dict

    @property
    def id(self) -> str:
        return self.raw["id"]

    @property
    def dimension(self) -> str | None:
        return self.raw.get("dimension")

    @property
    def inputs(self) -> list[str]:
        return list(self.raw["inputs"])

    @property
    def transform(self) -> dict:
        return self.raw["transform"]

    @property
    def params(self) -> dict:
        return self.raw["transform"].get("params", {}) or {}

    @property
    def normalize(self) -> dict:
        return self.raw["normalize"]

    @property
    def aggregation(self) -> str:
        return self.raw["aggregation"]

    @property
    def level(self) -> str:
        return self.raw.get("level", "cell")

    @property
    def scored(self) -> bool:
        return self.raw.get("scored", True) and self.dimension is not None

    @property
    def qa(self) -> list[dict]:
        return self.raw.get("qa", []) or []


@dataclass
class Catalog:
    data: dict
    paths: list[Path] = field(default_factory=list)

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, paths: list[str | Path], env: dict[str, str] | None = None) -> Catalog:
        merged: dict = {}
        for p in paths:
            layer = yaml.safe_load(Path(p).read_text()) or {}
            merged = merge(merged, layer)
        return cls(interpolate(merged, env), [Path(p) for p in paths])

    # ------------------------------------------------------------------ access
    @property
    def meta(self) -> dict:
        return self.data.get("meta", {})

    @property
    def base_resolution(self) -> int:
        return int(self.meta["grid"]["base_resolution"])

    @property
    def export_resolutions(self) -> list[int]:
        return list(self.meta["grid"].get("export_resolutions") or [self.base_resolution])

    @property
    def quant_scale(self) -> float:
        return float(self.meta.get("score_quantization", {}).get("scale", 25))

    @property
    def nodata(self) -> int:
        return int(self.meta.get("score_quantization", {}).get("nodata", 255))

    @property
    def sources(self) -> dict[str, Source]:
        return {s["id"]: Source(s) for s in self.data.get("sources", [])}

    @property
    def indicators(self) -> dict[str, Indicator]:
        return {i["id"]: Indicator(i) for i in self.data.get("indicators", [])}

    @property
    def dimensions(self) -> dict[str, dict]:
        return {d["id"]: d for d in self.data.get("dimensions", [])}

    @property
    def poi_groups(self) -> dict[str, dict]:
        return dict(self.data.get("osm_poi_groups", {}))

    def source(self, sid: str) -> Source:
        try:
            return self.sources[sid]
        except KeyError as e:
            raise CatalogError(f"unknown source {sid!r}") from e

    def indicator(self, iid: str) -> Indicator:
        try:
            return self.indicators[iid]
        except KeyError as e:
            raise CatalogError(f"unknown indicator {iid!r}") from e

    # ------------------------------------------------------------------ graph
    def downstream(self, source_ids: set[str]) -> list[str]:
        """Indicators that depend (directly) on any of ``source_ids``."""
        return [i.id for i in self.indicators.values() if set(i.inputs) & source_ids]

    def upstream(self, indicator_id: str) -> list[str]:
        return self.indicator(indicator_id).inputs

    def graph_edges(self) -> list[tuple[str, str]]:
        edges = []
        for ind in self.indicators.values():
            for s in ind.inputs:
                edges.append((f"source:{s}", f"indicator:{ind.id}"))
            if ind.dimension:
                edges.append((f"indicator:{ind.id}", f"dimension:{ind.dimension}"))
        return edges

    def graph(self, fmt: str = "mermaid") -> str:
        edges = self.graph_edges()
        if fmt == "dot":
            lines = ["digraph qualimap {", "  rankdir=LR;"]
            lines += [f'  "{a}" -> "{b}";' for a, b in edges]
            return "\n".join(lines + ["}"])
        safe = lambda s: s.replace(":", "_")  # noqa: E731
        lines = ["graph LR"]
        lines += [f"  {safe(a)}[{a}] --> {safe(b)}[{b}]" for a, b in edges]
        return "\n".join(lines)

    # ------------------------------------------------------------------ validate
    def validate(self) -> list[str]:
        """Return a list of human-readable problems (empty list = valid)."""
        errors: list[str] = []
        validator = jsonschema.Draft202012Validator(schema())
        for err in sorted(validator.iter_errors(self.data), key=lambda e: list(e.path)):
            loc = "/".join(str(p) for p in err.path) or "<root>"
            errors.append(f"schema: {loc}: {err.message}")
        if errors:
            return errors
        for section in ("sources", "indicators", "dimensions"):
            ids = [x["id"] for x in self.data.get(section, [])]
            dupes = {i for i in ids if ids.count(i) > 1}
            if dupes:
                errors.append(f"{section}: duplicate ids {sorted(dupes)}")
        sources, dims, groups = self.sources, self.dimensions, self.poi_groups
        boundary = self.meta.get("region", {}).get("boundary_source")
        if boundary and boundary not in sources:
            errors.append(f"meta.region.boundary_source: unknown source {boundary!r}")
        for ind in self.indicators.values():
            for s in ind.inputs:
                if s not in sources:
                    errors.append(f"indicator {ind.id}: unknown input {s!r}")
            if ind.dimension and ind.dimension not in dims:
                errors.append(f"indicator {ind.id}: unknown dimension {ind.dimension!r}")
            subtypes = ind.params.get("subtypes") or []
            for st in subtypes:
                if st.get("group") not in groups:
                    errors.append(f"indicator {ind.id}: unknown osm_poi_group {st.get('group')!r}")
            if subtypes:
                total = sum(float(st.get("weight", 0)) for st in subtypes)
                if abs(total - 1.0) > 1e-6:
                    errors.append(f"indicator {ind.id}: subtype weights sum to {total:.3f}, not 1")
            for q in ind.qa:
                if q["check"] not in KNOWN_QA_CHECKS:
                    errors.append(f"indicator {ind.id}: unknown qa check {q['check']!r}")
            if ":" not in ind.transform.get("fn", ""):
                errors.append(f"indicator {ind.id}: transform.fn must be 'module:function'")
        for d in dims:
            if not any(i.dimension == d for i in self.indicators.values()):
                errors.append(f"dimension {d}: no indicator feeds it")
        return errors

    def resolved_view(self) -> dict:
        """The public, resolved catalog published as ``catalog.json`` (no secrets, no fetch config)."""
        return {
            "catalog_version": self.meta.get("catalog_version"),
            "grid": self.meta.get("grid"),
            "languages": self.meta.get("languages"),
            "score_quantization": self.meta.get("score_quantization"),
            "dimensions": list(self.dimensions.values()),
            "indicators": [
                {
                    "id": i.id,
                    "dimension": i.dimension,
                    "scored": i.scored,
                    "title": i.raw.get("title"),
                    "unit": i.raw.get("unit"),
                    "normalize": i.normalize,
                    "column_q": f"q_{i.id}",
                    "column_raw": f"raw_{i.id}",
                }
                for i in self.indicators.values()
            ],
            "sources": [
                {
                    "id": s.id,
                    "provider": s.raw.get("provider"),
                    "title": s.raw.get("title"),
                    "license": s.raw.get("license"),
                    "raster_zarr": s.export_raster_zarr,
                }
                for s in self.sources.values()
            ],
        }


def merge(base: dict, layer: dict) -> dict:
    """Layer catalogs: list sections merge by ``id`` (later wins), mappings merge recursively."""
    out = dict(base)
    for key, value in layer.items():
        if key in ("sources", "indicators", "dimensions") and isinstance(value, list):
            existing = {x["id"]: x for x in out.get(key, [])}
            order = [x["id"] for x in out.get(key, [])]
            for item in value:
                if item["id"] not in existing:
                    order.append(item["id"])
                existing[item["id"]] = item
            out[key] = [existing[i] for i in order]
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out
