"""Fetch records, indicator build records and run records kept in the data store (SPEC §4.2, §5)."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .store import Store


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def ts_compact(dt: datetime | None = None) -> str:
    return (dt or utcnow()).strftime("%Y%m%dT%H%M%SZ")


def stable_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class FetchRecord:
    source_id: str
    fetched_at: str
    sha256: str
    files: list[dict]  # [{"key", "name", "bytes", "sha256", "url"}]
    etag: str | None = None
    last_modified: str | None = None
    upstream: dict = field(default_factory=dict)  # e.g. STAC asset checksums
    status: str = "ok"  # ok | stale | failed
    error: str | None = None

    @property
    def raw_prefix(self) -> str:
        return self.files[0]["key"].rsplit("/", 1)[0] if self.files else ""


class StateStore:
    def __init__(self, store: Store):
        self.store = store

    # ---------------------------------------------------------------- sources
    def fetch_record(self, source_id: str) -> FetchRecord | None:
        data = self.store.get_json(f"state/sources/{source_id}.json")
        return FetchRecord(**data) if data else None

    def save_fetch_record(self, rec: FetchRecord) -> None:
        self.store.put_json(f"state/sources/{rec.source_id}.json", asdict(rec))
        hist = self.store.get_json(f"state/sources/{rec.source_id}.history.json", []) or []
        hist.append({k: v for k, v in asdict(rec).items() if k != "files"} | {"raw_prefix": rec.raw_prefix})
        self.store.put_json(f"state/sources/{rec.source_id}.history.json", hist[-50:])

    def mark_source(self, source_id: str, status: str, error: str | None = None) -> None:
        rec = self.fetch_record(source_id)
        if rec:
            rec.status, rec.error = status, error
            self.store.put_json(f"state/sources/{source_id}.json", asdict(rec))

    def history(self, source_id: str) -> list[dict]:
        return self.store.get_json(f"state/sources/{source_id}.history.json", []) or []

    # ---------------------------------------------------------------- staging
    def staged_record(self, source_id: str) -> dict | None:
        return self.store.get_json(f"state/staged/{source_id}.json")

    def save_staged_record(self, source_id: str, record: dict) -> None:
        self.store.put_json(f"state/staged/{source_id}.json", record)

    # ---------------------------------------------------------------- indicators
    def indicator_record(self, indicator_id: str) -> dict | None:
        return self.store.get_json(f"state/indicators/{indicator_id}.json")

    def save_indicator_record(self, indicator_id: str, record: dict) -> None:
        self.store.put_json(f"state/indicators/{indicator_id}.json", record)

    # ---------------------------------------------------------------- runs
    def new_run(self, command: str, params: dict) -> RunRecord:
        return RunRecord(run_id=f"{ts_compact()}-{uuid.uuid4().hex[:6]}", command=command, params=params)

    def save_run(self, run: RunRecord) -> str:
        run.finished_at = utcnow().isoformat()
        key = f"runs/{run.run_id[:4]}/{run.run_id[4:6]}/{run.run_id}.json"
        self.store.put_json(key, asdict(run))
        self.store.put_json("state/last_run.json", asdict(run))
        return key

    def last_run(self) -> dict | None:
        return self.store.get_json("state/last_run.json")


@dataclass
class RunRecord:
    run_id: str
    command: str
    params: dict
    started_at: str = field(default_factory=lambda: utcnow().isoformat())
    finished_at: str | None = None
    steps: list[dict] = field(default_factory=list)
    outcome: str = "running"  # ok | failed | qa_failed | nothing_to_do
    release: str | None = None

    def step(self, name: str, **info: Any) -> None:
        self.steps.append({"step": name, "at": utcnow().isoformat(), **info})
