"""Maintenance: retention (gc), CORS rules for the public bucket, and environment checks (doctor)."""

from __future__ import annotations

from .catalog import unresolved
from .pipeline import Env
from .state import ts_compact


def gc(env: Env, keep_raw: int = 3, dry_run: bool = False) -> dict:
    """Apply retention: keep the last ``keep_raw`` raw fetches per source and only the staged,
    cell and indicator files referenced by current state records."""
    store, state = env.data, env.state
    removed: list[str] = []
    for sid in env.catalog.sources:
        stamps = store.list_dirs(f"raw/{sid}")
        current = state.fetch_record(sid)
        keep = set(stamps[-keep_raw:])
        if current and current.raw_prefix:
            keep.add(current.raw_prefix.rsplit("/", 1)[-1])
        for s in stamps:
            if s not in keep:
                removed.append(f"raw/{sid}/{s}")
        staged = state.staged_record(sid) or {}
        live = {k.split("/")[2] for k in staged.get("files", {}).values()}
        for d in store.list_dirs(f"staged/{sid}"):
            if d not in live:
                removed.append(f"staged/{sid}/{d}")
    for iid in env.catalog.indicators:
        rec = state.indicator_record(iid) or {}
        live = rec.get("key", "").split("/")[2] if rec.get("key") else None
        for d in store.list_dirs(f"indicators/{iid}"):
            if d != live:
                removed.append(f"indicators/{iid}/{d}")
    if not dry_run:
        for key in removed:
            store.delete(key)
    return {"removed": removed, "dry_run": dry_run}


def cors_rules(origins: list[str]) -> dict:
    return {"CORSRules": [{
        "AllowedOrigins": origins or ["*"],
        "AllowedMethods": ["GET", "HEAD"],
        "AllowedHeaders": ["Range", "If-None-Match", "If-Modified-Since"],
        "ExposeHeaders": ["Content-Length", "Content-Range", "ETag", "Accept-Ranges"],
        "MaxAgeSeconds": 86400,
    }]}


def cors_apply(env: Env, dry_run: bool = False) -> dict:
    s = env.settings
    rules = cors_rules(s.viewer_origins)
    if not env.pub.url.startswith("s3://"):
        return {"applied": False, "reason": "public store is not S3; local files need no CORS", "rules": rules}
    bucket = env.pub.url[len("s3://"):].split("/")[0]
    if dry_run:
        return {"applied": False, "bucket": bucket, "rules": rules, "dry_run": True}
    import boto3

    client = boto3.client("s3", endpoint_url=s.s3_endpoint, region_name=s.s3_region,
                          aws_access_key_id=s.s3_key_id, aws_secret_access_key=s.s3_secret)
    client.put_bucket_cors(Bucket=bucket, CORSConfiguration=rules)
    return {"applied": True, "bucket": bucket, "rules": rules}


def doctor(env: Env, check_network: bool = True) -> list[dict]:
    checks: list[dict] = []

    def add(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    errs = env.catalog.validate()
    add("catalog valid", not errs, "; ".join(errs[:3]))
    missing = sorted({n for s in env.catalog.sources.values() for n in unresolved(s.fetch) + unresolved(s.stage)})
    add("environment variables resolved", not missing, ", ".join(missing))
    for name, store in (("data store", env.data), ("public store", env.pub)):
        key = f"_doctor/{ts_compact()}.txt"
        try:
            store.put_bytes(key, b"ok")
            ok = store.get_bytes(key) == b"ok"
            store.delete(key)
            add(f"{name} read/write", ok, store.url)
        except Exception as e:
            add(f"{name} read/write", False, f"{store.url}: {e}")
    from . import duck

    con, info = duck.connect(load_a5=check_network)
    v = duck._version_tuple(info["duckdb"])
    add("DuckDB >= 1.5.6", v >= duck.MIN_DUCKDB, info["duckdb"])
    add("DuckDB spatial extension", info["spatial"], info.get("spatial_error") or "")
    add("DuckDB a5 extension (A5 v1)", info["a5"],
        info.get("a5_error") or ("" if check_network else "skipped (--offline)"))
    if info["a5"]:
        rows = duck.test_vectors(con)
        add("A5 IDs identical in DuckDB and pya5", all(r["match"] for r in rows),
            ", ".join(f"{r['pya5']}" for r in rows))
    import a5

    add("pya5 >= 1.0", _pya5_ok(), getattr(a5, "BACKEND", ""))
    if env.pub.url.startswith("s3://") and check_network:
        try:
            import boto3

            s = env.settings
            client = boto3.client("s3", endpoint_url=s.s3_endpoint, region_name=s.s3_region,
                                  aws_access_key_id=s.s3_key_id, aws_secret_access_key=s.s3_secret)
            cfg = client.get_bucket_cors(Bucket=env.pub.url[5:].split("/")[0])
            ranged = any("Range" in r.get("AllowedHeaders", []) for r in cfg.get("CORSRules", []))
            add("public bucket CORS allows Range", ranged)
        except Exception as e:
            add("public bucket CORS allows Range", False, str(e).splitlines()[0])
    return checks


def _pya5_ok() -> bool:
    from importlib.metadata import version

    try:
        return int(version("pya5").split(".")[0]) >= 1
    except Exception:
        return False
