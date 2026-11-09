"""End-to-end orchestration: fetch → stage → cells → compute → export → publish (SPEC §5)."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import releases
from .catalog import Catalog
from .compute import compute_indicator, is_dirty
from .context import Context
from .export import build_release
from .fetchers import FetchContext, get_fetcher
from .population import build_cells
from .settings import Settings
from .staging import stage_source
from .state import RunRecord, StateStore
from .store import Store, data_store, pub_store

STAGES = ("fetch", "stage", "cells", "compute", "export", "publish")
DEFERRED_METHODS = {"estv_api"}
EXIT_OK, EXIT_FAIL, EXIT_QA, EXIT_NOTHING = 0, 1, 2, 3


@dataclass
class Env:
    settings: Settings
    catalog: Catalog
    data: Store
    pub: Store
    state: StateStore
    client: httpx.Client | None = None
    log: list[str] = field(default_factory=list)

    @classmethod
    def create(cls, settings: Settings, client: httpx.Client | None = None) -> Env:
        cat = Catalog.load(settings.catalog_paths)
        data = data_store(settings)
        return cls(settings, cat, data, pub_store(settings), StateStore(data), client)

    def fetch_ctx(self) -> FetchContext:
        return FetchContext(self.catalog, self.data, self.state, client=self.client,
                            workdir=self.settings.cache_dir / "fetch")

    def context(self, res: int | None = None) -> Context:
        return Context(self.catalog, self.data, self.state, res or self.catalog.base_resolution)

    def say(self, msg: str) -> None:
        self.log.append(msg)


def fetch(env: Env, ids: list[str], force: bool = False, due_only: bool = False) -> list[dict]:
    out = []
    fctx = env.fetch_ctx()
    for sid in ids:
        src = env.catalog.source(sid)
        fetcher = get_fetcher(src.method, src.fetch.get("class"))(src, fctx)
        if due_only and not force and not fetcher.is_due():
            out.append({"source": sid, "changed": False, "message": "not due"})
            continue
        try:
            r = fetcher.fetch(force=force)
            out.append({"source": sid, "changed": r.changed, "message": r.message})
        except Exception as e:
            out.append({"source": sid, "changed": False, "error": str(e)})
    return out


def stage(env: Env, ids: list[str], force: bool = False) -> list[dict]:
    out = []
    for sid in ids:
        try:
            rec = stage_source(env.catalog, env.data, env.state, sid, force=force)
            out.append({"source": sid, "unchanged": rec.get("unchanged", False),
                        "files": sorted(rec.get("files", {})), "skipped": rec.get("skipped")})
        except Exception as e:
            out.append({"source": sid, "error": str(e)})
    return out


def cells(env: Env, res: int | None = None, bbox=None) -> dict:
    return build_cells(env.catalog, env.data, env.state, res, bbox)


def compute(env: Env, ids: list[str], force: bool = False, dirty_only: bool = False,
            res: int | None = None) -> list[dict]:
    ctx = env.context(res)
    out = []
    for iid in ids:
        ind = env.catalog.indicator(iid)
        if dirty_only and not force and not is_dirty(env.catalog, ind, env.state, env.data, ctx.res):
            out.append({"indicator": iid, "status": "clean"})
            continue
        try:
            rec = compute_indicator(ctx, ind, force=force)
            out.append({"indicator": iid, "status": "unchanged" if rec.get("unchanged") else "computed",
                        "qa_passed": rec.get("qa_passed"), "stats": rec.get("stats"), "qa": rec.get("qa"),
                        "notes": rec.get("notes")})
        except Exception as e:
            out.append({"indicator": iid, "status": "error", "error": str(e)})
    return out


def export(env: Env, version: str | None = None, formats=("parquet", "zarr", "raster-zarr"),
           resolutions: list[int] | None = None, out_dir: Path | None = None) -> tuple[str, Path, dict]:
    version = version or releases.next_version(env.pub)
    out_dir = out_dir or Path(tempfile.mkdtemp(prefix=f"qm-release-{version}-"))
    manifest = build_release(env.catalog, env.data, env.state, out_dir, version, tuple(formats), resolutions)
    return version, out_dir, manifest


def run(env: Env, mode: str = "due", until: str = "publish", force: bool = False,
        max_mean_shift: float = 1.0, switch: bool = True) -> tuple[int, RunRecord]:
    """``qm run``: returns (exit code, run record)."""
    run = env.state.new_run("run", {"mode": mode, "until": until, "force": force})
    try:
        ids = [s for s, src in env.catalog.sources.items() if src.method != "none"]
        # sources whose fetch needs staged data from other sources (ESTV needs the commune list)
        deferred = [s for s in ids if env.catalog.source(s).method in DEFERRED_METHODS]
        first = [s for s in ids if s not in deferred]
        fetched = fetch(env, first, force=force, due_only=(mode == "due"))
        changed = {r["source"] for r in fetched if r.get("changed")}
        if until == "fetch" and not deferred:
            run.step("fetch", results=fetched)
            return _finish(env, run, EXIT_OK)
        staged = stage(env, _to_stage(env, first, changed, mode), force=force)
        later = fetch(env, deferred, force=force, due_only=(mode == "due"))
        fetched += later
        changed |= {r["source"] for r in later if r.get("changed")}
        staged += stage(env, _to_stage(env, deferred, changed, mode), force=force)
        run.step("fetch", results=fetched)
        run.step("stage", results=staged)
        if until in ("fetch", "stage"):
            return _finish(env, run, EXIT_OK)
        res = env.catalog.base_resolution
        cell_inputs = {env.catalog.meta.get("region", {}).get("boundary_source"), "fso_gwr", "fso_statpop"}
        if not env.data.exists(f"cells/universe_r{res}.parquet") or (changed & cell_inputs):
            run.step("cells", result=cells(env, res))
        if until == "cells":
            return _finish(env, run, EXIT_OK)
        inds = list(env.catalog.indicators)
        computed = compute(env, inds, force=force, dirty_only=True)
        run.step("compute", results=computed)
        new = [c for c in computed if c["status"] == "computed"]
        errors = [c for c in computed if c["status"] == "error"]
        if until == "compute":
            return _finish(env, run, EXIT_FAIL if errors else EXIT_OK)
        if not new and mode == "due" and not force:
            return _finish(env, run, EXIT_NOTHING)
        qa_failed = [c["indicator"] for c in computed if c.get("qa_passed") is False]
        version, out_dir, manifest = export(env)
        run.step("export", version=version, dir=str(out_dir), files=len(manifest["files"]))
        if until == "export":
            return _finish(env, run, EXIT_QA if qa_failed else EXIT_OK)
        if qa_failed:
            run.step("publish", skipped=f"QA failed for {qa_failed}")
            return _finish(env, run, EXIT_QA)
        prev = releases.latest(env.pub)
        do_switch = switch
        if prev:
            res_cmp = 13 if 13 in manifest["resolutions"] else min(manifest["resolutions"])
            try:
                d = releases.diff_local(env.pub, prev["version"], out_dir, res_cmp)
            except Exception as e:  # previous release without this resolution
                run.step("diff", error=str(e))
                d = None
            if d is not None and len(d):
                big = d[(d.get("status") == "ok") & (d["mean_abs_shift"].fillna(0) > max_mean_shift)] \
                    if "mean_abs_shift" in d else d.iloc[0:0]
                run.step("diff", against=prev["version"], rows=d.to_dict("records"))
                if len(big):
                    do_switch = False
                    run.step("diff_block", indicators=big["indicator"].tolist())
        pub = releases.publish(env.pub, out_dir, version, switch=do_switch,
                               public_base_url=env.settings.public_base_url)
        run.release = version
        run.step("publish", **pub)
        return _finish(env, run, EXIT_OK if do_switch else EXIT_QA)
    except Exception as e:
        run.step("error", error=str(e))
        return _finish(env, run, EXIT_FAIL)


def _to_stage(env: Env, ids: list[str], changed: set[str], mode: str) -> list[str]:
    return sorted(s for s in ids if env.state.fetch_record(s)
                  and (s in changed or mode == "all" or not env.state.staged_record(s)))


def _finish(env: Env, run: RunRecord, code: int) -> tuple[int, RunRecord]:
    run.outcome = {EXIT_OK: "ok", EXIT_FAIL: "failed", EXIT_QA: "qa_failed", EXIT_NOTHING: "nothing_to_do"}[code]
    env.state.save_run(run)
    if code in (EXIT_FAIL, EXIT_QA) and env.settings.alert_webhook:
        try:
            httpx.post(env.settings.alert_webhook, json={"text": f"qualimap run {run.run_id}: {run.outcome}",
                                                        "run_id": run.run_id, "outcome": run.outcome}, timeout=10)
        except httpx.HTTPError:
            pass
    return code, run
