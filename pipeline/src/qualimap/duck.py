"""DuckDB connection with ``spatial`` and the A5 v1 extension (SPEC §3.3)."""

from __future__ import annotations

import duckdb

MIN_DUCKDB = (1, 5, 6)
TEST_POINTS = [(8.5417, 47.3769), (6.6323, 46.5197), (8.9511, 46.0037), (7.4474, 46.9480)]


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int("".join(ch for ch in p if ch.isdigit()) or 0) for p in v.lstrip("v").split(".")[:3])


def connect(path: str = ":memory:", load_a5: bool = True) -> tuple[duckdb.DuckDBPyConnection, dict]:
    con = duckdb.connect(path)
    info = {"duckdb": duckdb.__version__, "spatial": False, "a5": False, "a5_error": None}
    try:
        con.sql("INSTALL spatial; LOAD spatial;")
        con.sql("SET geometry_always_xy = true;")
        info["spatial"] = True
    except Exception as e:
        info["spatial_error"] = str(e).splitlines()[0]
    if load_a5:
        info["a5"], info["a5_error"] = _load_a5(con)
    return con, info


def _load_a5(con) -> tuple[bool, str | None]:
    v = _version_tuple(duckdb.__version__)
    if v < MIN_DUCKDB:
        return False, f"DuckDB {duckdb.__version__} < 1.5.6 would install legacy v0 A5 IDs"
    attempts = (
        ["INSTALL a5 FROM community", "LOAD a5"]
        if v < (2, 0, 0)
        else ["INSTALL a5 FROM query_farm", "LOAD a5 FROM query_farm"]
    )
    fallback = ["INSTALL a5 FROM community", "LOAD a5"]
    for stmts in (attempts, fallback):
        try:
            for s in stmts:
                con.sql(s)
            return True, None
        except Exception as e:
            err = str(e).splitlines()[0]
    return False, err


def test_vectors(con, res: int = 16) -> list[dict]:
    """Compare DuckDB A5 IDs with pya5 for fixed points (``qm doctor``)."""
    import a5

    rows = []
    for lon, lat in TEST_POINTS:
        py = int(a5.lonlat_to_cell((lon, lat), res))
        dk = int(con.sql(f"SELECT a5_lonlat_to_cell({lon}, {lat}, {res})").fetchone()[0])
        rows.append({"lon": lon, "lat": lat, "pya5": hex(py), "duckdb": hex(dk), "match": py == dk})
    return rows


def register_release(con, base_url: str, resolutions: list[int]) -> None:
    """Create views ``cells_r{res}`` and ``communes`` over a release (local path or URL)."""
    base = base_url.rstrip("/")
    for r in resolutions:
        con.sql(f"CREATE OR REPLACE VIEW cells_r{r} AS SELECT * FROM read_parquet('{base}/parquet/cells_r{r}.parquet')")
    con.sql(f"CREATE OR REPLACE VIEW communes AS SELECT * FROM read_parquet('{base}/parquet/communes.parquet')")
