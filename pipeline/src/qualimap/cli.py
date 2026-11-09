"""``qm``: the Qualimap command line (SPEC §8)."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import pipeline as P
from . import releases as R
from .catalog import Catalog
from .settings import Settings, load_env_file

app = typer.Typer(help="Qualimap pipeline and maintenance CLI.", no_args_is_help=True, add_completion=False)
sources_app = typer.Typer(help="List and inspect data sources.", no_args_is_help=True)
indicators_app = typer.Typer(help="List and inspect indicators.", no_args_is_help=True)
catalog_app = typer.Typer(help="Validate and visualise the catalog.", no_args_is_help=True)
cells_app = typer.Typer(help="Build the A5 cell universe.", no_args_is_help=True)
releases_app = typer.Typer(help="Published releases.", no_args_is_help=True)
qa_app = typer.Typer(help="Quality assurance.", no_args_is_help=True)
inspect_app = typer.Typer(help="Inspect cells and communes.", no_args_is_help=True)
cors_app = typer.Typer(help="CORS rules for the public bucket.", no_args_is_help=True)
for name, sub in [("sources", sources_app), ("indicators", indicators_app), ("catalog", catalog_app),
                  ("cells", cells_app), ("releases", releases_app), ("qa", qa_app),
                  ("inspect", inspect_app), ("cors", cors_app)]:
    app.add_typer(sub, name=name)

console = Console()


@dataclass
class Opts:
    catalog: list[str] | None
    as_json: bool
    dry_run: bool
    verbose: bool
    _env: P.Env | None = None

    @property
    def env(self) -> P.Env:
        if self._env is None:
            self._env = P.Env.create(Settings.from_env(self.catalog))
        return self._env


def _opts(ctx: typer.Context) -> Opts:
    return ctx.obj


def _emit(o: Opts, data, columns: list[str] | None = None, title: str | None = None) -> None:
    if o.as_json:
        typer.echo(json.dumps(data, indent=2, default=str))
        return
    if isinstance(data, list) and data and isinstance(data[0], dict):
        cols = columns or list(data[0].keys())
        t = Table(title=title, show_lines=False, header_style="bold")
        for c in cols:
            t.add_column(c.upper().replace("_", " "))
        for row in data:
            t.add_row(*[_fmt(row.get(c)) for c in cols])
        console.print(t)
    elif isinstance(data, dict):
        for k, v in data.items():
            console.print(f"[bold]{k}[/bold]: {_fmt(v) if not isinstance(v, (dict, list)) else json.dumps(v, default=str)}")
    elif isinstance(data, list) and not data:
        console.print("(none)")
    else:
        console.print(data)


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v)


@app.callback()
def main_callback(
    ctx: typer.Context,
    catalog: list[str] = typer.Option(None, "--catalog", help="Catalog YAML (repeatable; later files override)."),
    env_file: Path = typer.Option(Path(".env"), "--env", help="Environment file to load."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    load_env_file(env_file)
    ctx.obj = Opts(catalog, as_json, dry_run, verbose)


# ======================================================================== sources
@sources_app.command("list")
def sources_list(ctx: typer.Context, provider: str = typer.Option(None), stale: bool = False, due: bool = False):
    """All sources with provider, method, refresh policy, last fetch and status."""
    o = _opts(ctx)
    env = o.env
    fctx = env.fetch_ctx()
    from .fetchers import get_fetcher

    rows = []
    for s in env.catalog.sources.values():
        if provider and provider.lower() not in str(s.raw.get("provider", "")).lower():
            continue
        rec = env.state.fetch_record(s.id)
        is_due = get_fetcher(s.method, s.fetch.get("class"))(s, fctx).is_due() if s.method != "custom" else None
        status = rec.status if rec else ("n/a" if s.method == "none" else "never")
        if due and not is_due:
            continue
        if stale and status not in ("stale", "failed"):
            continue
        rows.append({"id": s.id, "provider": s.raw.get("provider"), "method": s.method,
                     "refresh": s.raw["refresh"]["every"], "last_fetch": rec.fetched_at if rec else None,
                     "status": "due" if is_due and rec else status})
    _emit(o, rows)


@sources_app.command("show")
def sources_show(ctx: typer.Context, source_id: str):
    """Full catalog entry, last fetch record and downstream indicators."""
    o = _opts(ctx)
    env = o.env
    src = env.catalog.source(source_id)
    rec = env.state.fetch_record(source_id)
    data = {"entry": src.raw, "fetch_record": rec.__dict__ if rec else None,
            "staged": env.state.staged_record(source_id),
            "downstream": env.catalog.downstream({source_id}),
            "history": env.state.history(source_id)[-5:]}
    if o.as_json:
        _emit(o, data)
    else:
        import yaml

        console.print(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


# ======================================================================== indicators
@indicators_app.command("list")
def indicators_list(ctx: typer.Context, dimension: str = typer.Option(None)):
    o = _opts(ctx)
    env = o.env
    rows = []
    for i in env.catalog.indicators.values():
        if dimension and i.dimension != dimension:
            continue
        rec = env.state.indicator_record(i.id) or {}
        st = rec.get("stats") or {}
        rows.append({"id": i.id, "dimension": i.dimension, "inputs": len(i.inputs),
                     "normalize": i.normalize["method"], "last_build": rec.get("computed_at"),
                     "raw_min": st.get("raw_min"), "raw_max": st.get("raw_max"), "qa": rec.get("qa_passed")})
    _emit(o, rows)


@indicators_app.command("show")
def indicators_show(ctx: typer.Context, indicator_id: str):
    o = _opts(ctx)
    env = o.env
    ind = env.catalog.indicator(indicator_id)
    lineage = {s: (env.state.staged_record(s) or {}).get("raw_sha256") for s in ind.inputs}
    data = {"definition": ind.raw, "lineage": lineage, "last_build": env.state.indicator_record(indicator_id)}
    if o.as_json:
        _emit(o, data)
    else:
        import yaml

        console.print(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


# ======================================================================== catalog
@catalog_app.command("validate")
def catalog_validate(ctx: typer.Context):
    """Validate YAML against the JSON Schema and check the dependency graph."""
    o = _opts(ctx)
    settings = Settings.from_env(o.catalog)
    errors = Catalog.load(settings.catalog_paths).validate()
    if o.as_json:
        _emit(o, {"valid": not errors, "errors": errors})
    elif errors:
        for e in errors:
            console.print(f"[red]✗[/red] {e}")
    else:
        console.print(f"[green]✓[/green] catalog valid ({', '.join(str(p) for p in settings.catalog_paths)})")
    raise typer.Exit(1 if errors else 0)


@catalog_app.command("graph")
def catalog_graph(ctx: typer.Context, fmt: str = typer.Option("mermaid", "--format", help="mermaid | dot")):
    o = _opts(ctx)
    typer.echo(o.env.catalog.graph(fmt))


# ======================================================================== status
@app.command()
def status(ctx: typer.Context):
    """Due sources, dirty indicators, latest release, last run outcome."""
    o = _opts(ctx)
    env = o.env
    from .compute import is_dirty
    from .fetchers import get_fetcher

    fctx = env.fetch_ctx()
    due = [s.id for s in env.catalog.sources.values()
           if s.method not in ("none", "custom") and get_fetcher(s.method)(s, fctx).is_due()]
    res = env.catalog.base_resolution
    dirty = [i.id for i in env.catalog.indicators.values()
             if is_dirty(env.catalog, i, env.state, env.data, res)]
    last = env.state.last_run() or {}
    data = {"data_store": env.data.url, "public_store": env.pub.url, "due_sources": due,
            "dirty_indicators": dirty, "latest_release": (R.latest(env.pub) or {}).get("version"),
            "last_run": {k: last.get(k) for k in ("run_id", "outcome", "finished_at", "release")} if last else None}
    _emit(o, data)


# ======================================================================== pipeline
def _ids(env: P.Env, ids: list[str] | None, all_: bool, kind: str) -> list[str]:
    pool = env.catalog.sources if kind == "source" else env.catalog.indicators
    if all_:
        return [i for i, s in pool.items() if kind != "source" or s.method != "none"]
    if not ids:
        raise typer.BadParameter(f"name at least one {kind} or pass --all")
    for i in ids:
        if i not in pool:
            raise typer.BadParameter(f"unknown {kind} {i!r}")
    return list(ids)


@app.command()
def fetch(ctx: typer.Context, ids: list[str] = typer.Argument(None), all_: bool = typer.Option(False, "--all"),
          due: bool = False, force: bool = False):
    """Fetch sources if upstream changed."""
    o = _opts(ctx)
    env = o.env
    targets = _ids(env, ids, all_ or due, "source")
    if o.dry_run:
        _emit(o, [{"source": s, "action": "fetch"} for s in targets])
        return
    res = P.fetch(env, targets, force=force, due_only=due)
    _emit(o, res)
    raise typer.Exit(1 if any(r.get("error") for r in res) else 0)


@app.command()
def stage(ctx: typer.Context, ids: list[str] = typer.Argument(None), all_: bool = typer.Option(False, "--all"),
          force: bool = False):
    """Normalise raw files into staged Parquet / COG."""
    o = _opts(ctx)
    env = o.env
    targets = _ids(env, ids, all_, "source")
    if all_:
        targets = [s for s in targets if env.state.fetch_record(s)]
    res = P.stage(env, targets, force=force)
    _emit(o, res)
    raise typer.Exit(1 if any(r.get("error") for r in res) else 0)


@app.command()
def reingest(ctx: typer.Context, ids: list[str] = typer.Argument(None), all_: bool = typer.Option(False, "--all"),
             from_raw: bool = typer.Option(False, "--from-raw", help="Re-stage existing raw files, no download.")):
    """Force fetch + stage for sources (or re-stage from raw), then mark downstream dirty."""
    o = _opts(ctx)
    env = o.env
    targets = _ids(env, ids, all_, "source")
    out = []
    if not from_raw:
        out += [r | {"step": "fetch"} for r in P.fetch(env, targets, force=True)]
    out += [r | {"step": "stage"} for r in P.stage(env, targets, force=True)]
    downstream = env.catalog.downstream(set(targets))
    for iid in downstream:
        env.state.save_indicator_record(iid, (env.state.indicator_record(iid) or {}) | {"hash": "dirty"})
    _emit(o, out)
    console.print(f"downstream marked dirty: {', '.join(downstream) or '-'}") if not o.as_json else None
    raise typer.Exit(1 if any(r.get("error") for r in out) else 0)


@cells_app.command("build")
def cells_build(ctx: typer.Context, res: int = typer.Option(None, help="Resolution (default: catalog base)."),
                bbox: str = typer.Option(None, help="lon1,lat1,lon2,lat2 (local test runs)")):
    """Build the cell universe and building/population tables."""
    o = _opts(ctx)
    bb = tuple(float(v) for v in bbox.split(",")) if bbox else None
    _emit(o, P.cells(o.env, res, bb))


@app.command()
def compute(ctx: typer.Context, ids: list[str] = typer.Argument(None), all_: bool = typer.Option(False, "--all"),
            dirty: bool = False, force: bool = False, res: int = typer.Option(None)):
    """Compute indicators (cached by input hash)."""
    o = _opts(ctx)
    env = o.env
    targets = _ids(env, ids, all_ or dirty, "indicator")
    out = P.compute(env, targets, force=force, dirty_only=dirty, res=res)
    _emit(o, [{k: r.get(k) for k in ("indicator", "status", "qa_passed", "error")} for r in out]
          if not o.as_json else out)
    if any(r["status"] == "error" for r in out):
        raise typer.Exit(1)
    if any(r.get("qa_passed") is False for r in out):
        raise typer.Exit(2)


@app.command()
def export(ctx: typer.Context, version: str = typer.Option(None),
           formats: str = typer.Option("parquet,zarr,raster-zarr"), res: str = typer.Option(None, help="e.g. 11-16"),
           out: Path = typer.Option(None, help="Output directory (default: temporary).")):
    """Write GeoParquet, A5 GeoZarr and raster GeoZarr for a release."""
    o = _opts(ctx)
    resolutions = None
    if res:
        a, _, b = res.partition("-")
        resolutions = list(range(int(a), int(b or a) + 1))
    v, d, m = P.export(o.env, version, tuple(f.strip() for f in formats.split(",")), resolutions, out)
    _emit(o, {"version": v, "dir": str(d), "files": len(m["files"]), "resolutions": m["resolutions"]})


@app.command()
def publish(ctx: typer.Context, version: str, directory: Path = typer.Option(..., "--dir"),
            no_switch: bool = typer.Option(False, "--no-switch")):
    """Upload a release directory and switch ``latest.json``."""
    o = _opts(ctx)
    env = o.env
    _emit(o, R.publish(env.pub, directory, version, switch=not no_switch,
                       public_base_url=env.settings.public_base_url))


@app.command()
def run(ctx: typer.Context, due: bool = typer.Option(True, "--due/--all"),
        until: str = typer.Option("publish", help="stage | cells | compute | export | publish"),
        force: bool = False, max_mean_shift: float = typer.Option(1.0, help="Block latest switch above this mean |Δscore|.")):
    """End-to-end: fetch due sources → rebuild affected indicators → export → publish."""
    o = _opts(ctx)
    code, record = P.run(o.env, "due" if due else "all", until, force, max_mean_shift)
    if o.as_json:
        _emit(o, record.__dict__)
    else:
        for s in record.steps:
            console.print(f"[bold]{s['step']}[/bold] {json.dumps({k: v for k, v in s.items() if k not in ('step', 'at', 'results', 'rows')}, default=str)}")
        console.print(f"outcome: [bold]{record.outcome}[/bold] (run {record.run_id})")
    raise typer.Exit(code)


# ======================================================================== releases
@releases_app.command("list")
def releases_list(ctx: typer.Context):
    o = _opts(ctx)
    env = o.env
    latest = (R.latest(env.pub) or {}).get("version")
    rows = [r | {"latest": r["version"] == latest, "size_mb": round(r["bytes"] / 1e6, 1)} for r in R.list_releases(env.pub)]
    _emit(o, rows, ["version", "created_at", "size_mb", "latest", "git_sha"])


@releases_app.command("rollback")
def releases_rollback(ctx: typer.Context, version: str):
    """Point ``latest.json`` at an earlier release."""
    o = _opts(ctx)
    env = o.env
    _emit(o, R.switch_latest(env.pub, version, env.settings.public_base_url))


@releases_app.command("diff")
def releases_diff(ctx: typer.Context, a: str, b: str, res: int = 13, commune: int = typer.Option(None)):
    """Per-indicator change statistics between two releases."""
    o = _opts(ctx)
    df = R.diff(o.env.pub, a, b, res, commune)
    _emit(o, df.to_dict("records"))


# ======================================================================== qa
@qa_app.command("run")
def qa_run(ctx: typer.Context, indicator: list[str] = typer.Option(None),
           compare_neighbourhoods: str = typer.Option(None, "--compare-neighbourhoods",
                                                       help="Indicator to compute with each neighbourhood method.")):
    """Run QA checks without publishing (and optionally compare neighbourhood methods)."""
    o = _opts(ctx)
    env = o.env
    if compare_neighbourhoods:
        from .qa_tools import compare_neighbourhoods as cmp

        _emit(o, cmp(env, compare_neighbourhoods))
        return
    from . import qa
    from .compute import load_indicator

    ctx_ = env.context()
    rows = []
    for iid in indicator or list(env.catalog.indicators):
        df = load_indicator(env.data, env.state, iid)
        if df is None:
            rows.append({"indicator": iid, "check": "-", "status": "not computed", "detail": ""})
            continue
        ind = env.catalog.indicator(iid)
        for r in qa.run_checks(ind, df, ctx_, env.state.indicator_record(iid)):
            rows.append({"indicator": iid} | r)
    _emit(o, rows)
    raise typer.Exit(2 if any(r["status"] == "fail" for r in rows) else 0)


# ======================================================================== inspect
@inspect_app.command("cell")
def inspect_cell(ctx: typer.Context, target: str = typer.Argument(..., help="cell id (int or 0x-hex) or lon,lat"),
                 res: int = typer.Option(None)):
    """All indicator values and lineage for one cell."""
    o = _opts(ctx)
    env = o.env
    from .inspect_tools import inspect_cell as ic

    _emit(o, ic(env, target, res))


@inspect_app.command("commune")
def inspect_commune(ctx: typer.Context, target: str = typer.Argument(..., help="BFS number or name")):
    """Commune values and ranks (latest release)."""
    o = _opts(ctx)
    from .inspect_tools import inspect_commune as icm

    _emit(o, icm(o.env, target))


# ======================================================================== maintenance
@app.command()
def gc(ctx: typer.Context, keep_raw: int = 3):
    """Apply retention rules to raw/, staged/ and indicators/."""
    o = _opts(ctx)
    from .maintenance import gc as do_gc

    r = do_gc(o.env, keep_raw, o.dry_run)
    _emit(o, {"removed": len(r["removed"]), "dry_run": r["dry_run"], "keys": r["removed"][:50]})


@cors_app.command("apply")
def cors_apply(ctx: typer.Context):
    """Apply the CORS rules (GET/HEAD with Range) to the public bucket."""
    o = _opts(ctx)
    from .maintenance import cors_apply as ca

    _emit(o, ca(o.env, o.dry_run))


@app.command()
def doctor(ctx: typer.Context, offline: bool = typer.Option(False, help="Skip network checks.")):
    """Check credentials, buckets, CORS, DuckDB + A5 versions and cross-library ID test vectors."""
    o = _opts(ctx)
    from .maintenance import doctor as dr

    rows = dr(o.env, check_network=not offline)
    _emit(o, rows, ["check", "ok", "detail"])
    raise typer.Exit(0 if all(r["ok"] for r in rows) else 1)


@app.command()
def shell(ctx: typer.Context, release: str = typer.Option(None, help="Register views for a published release.")):
    """Interactive Python prompt with catalog, store, duck (DuckDB) and a5 preloaded."""
    o = _opts(ctx)
    env = o.env
    import a5

    from . import duck as duckmod

    duck, info = duckmod.connect()
    ns = {"env": env, "catalog": env.catalog, "store": env.data, "pub": env.pub, "state": env.state,
          "duck": duck, "a5": a5}
    if release:
        base = env.pub.path(f"releases/{release}") if env.pub.is_local else env.pub.url_for(f"releases/{release}")
        duckmod.register_release(duck, base, env.catalog.export_resolutions)
        ns["release"] = release
    banner = (f"qualimap shell — catalog {env.catalog.meta.get('catalog_version')}, data {env.data.url}\n"
              f"objects: env, catalog, store, pub, state, duck (a5 ext: {info['a5']}), a5"
              + (f"; views cells_r*, communes for {release}" if release else ""))
    try:
        _ipython(ns, banner)
    except ImportError:
        import code

        code.interact(banner=banner, local=ns)


def _ipython(ns, banner):
    from IPython.terminal.embed import InteractiveShellEmbed

    InteractiveShellEmbed(banner1=banner, user_ns=ns)()


@app.command()
def demo(ctx: typer.Context, bbox: str = typer.Option("8.47,47.35,8.59,47.41"), res: int = 16,
         version: str = typer.Option(None), no_switch: bool = typer.Option(False, "--no-switch")):
    """Publish a synthetic release (production layout) for viewer development."""
    o = _opts(ctx)
    env = o.env
    from .demo import build_demo

    bb = tuple(float(v) for v in bbox.split(","))
    _emit(o, build_demo(env.catalog, env.pub, bb, res, version, not no_switch, env.settings.public_base_url))


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()

