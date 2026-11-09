"""Fetchers download upstream data into ``raw/`` and record what they fetched (SPEC §5.2)."""

from __future__ import annotations

from importlib.metadata import entry_points

from .base import FetchContext, Fetcher, FetchResult
from .estv import EstvFetcher
from .http import GtfsFetcher, HttpFetcher, OsmPbfFetcher
from .none import NoneFetcher
from .stac import StacFetcher

BUILTIN: dict[str, type[Fetcher]] = {
    "http": HttpFetcher,
    "stac": StacFetcher,
    "osm_pbf": OsmPbfFetcher,
    "gtfs": GtfsFetcher,
    "estv_api": EstvFetcher,
    "none": NoneFetcher,
}


def get_fetcher(method: str, class_path: str | None = None) -> type[Fetcher]:
    if method == "custom":
        if not class_path:
            raise ValueError("fetch.method=custom needs fetch.class")
        for ep in entry_points(group="qualimap.fetchers"):
            if ep.name == class_path:
                return ep.load()
        module, _, name = class_path.partition(":")
        import importlib

        return getattr(importlib.import_module(module), name)
    try:
        return BUILTIN[method]
    except KeyError as e:
        raise ValueError(f"unknown fetch method {method!r}") from e


__all__ = ["FetchContext", "FetchResult", "Fetcher", "get_fetcher", "BUILTIN"]
