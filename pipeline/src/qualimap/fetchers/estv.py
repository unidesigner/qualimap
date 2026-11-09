"""FTA (ESTV) tax calculator fetcher (SPEC §3.4 of the methodology).

The calculator's JSON interface is undocumented; every field name and enum code used here is
configurable in the catalog (``fetch.params``) so a change upstream is a catalog edit. Verify
codes once per tax year with ``qm sources show fta_tax_calculator`` and the golden-value test.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..state import FetchRecord
from .base import Fetcher

DEFAULT_FIELDS = {
    "location_id": "TaxLocationID",
    "bfs": "BfsID",
    "name": "City",
    "canton": "Canton",
}
DEFAULT_COMPONENTS = ["IncomeTaxFed", "IncomeTaxCanton", "IncomeTaxCity", "IncomeTaxChurch",
                      "FortuneTaxCanton", "FortuneTaxCity", "FortuneTaxChurch"]


class EstvFetcher(Fetcher):
    def communes(self) -> list[dict]:
        from ..staging import load_communes  # local import: staging depends on fetch records

        df = load_communes(self.ctx.catalog, self.ctx.store, self.ctx.state)
        return df[["commune_bfs", "name"]].drop_duplicates().to_dict("records")

    def post(self, operation: str, body: dict) -> dict:
        base = self.source.fetch["base_url"].rstrip("/")
        r = self.ctx.http().post(f"{base}/{operation}", json=body)
        r.raise_for_status()
        try:
            return r.json().get("response", r.json())
        except json.JSONDecodeError as e:  # bad input returns an HTML 500 page
            raise RuntimeError(f"ESTV {operation}: non-JSON response") from e

    def payload(self, location_id: int, p: dict) -> dict:
        enums = p.get("enums", {})
        rel = enums.get("relationship", {}).get(p.get("relationship", "married"), 2)
        conf = enums.get("confession", {}).get(p.get("confession", "none"), 0)
        rtype = enums.get("revenue_type", {}).get(p.get("revenue_type", "employed"), 1)
        return {
            "SimKey": None,
            "TaxYear": int(p["tax_year"]),
            "TaxLocationID": int(location_id),
            "Relationship": rel,
            "Confession1": conf,
            "Confession2": conf if p.get("revenue2") else 0,
            "Children": [{"Age": int(a)} for a in p.get("children_ages", [])],
            "Age1": int(p.get("age1", 40)),
            "RevenueType1": rtype,
            "Revenue1": float(p.get("revenue1", 0)),
            "Age2": int(p.get("age2", 0)),
            "RevenueType2": rtype if p.get("revenue2") else 0,
            "Revenue2": float(p.get("revenue2", 0)),
            "Fortune": float(p.get("fortune", 0)),
        }

    def download(self, workdir: Path, prev: FetchRecord | None, force: bool):
        p = dict(self.source.fetch.get("params", {}))
        fields = DEFAULT_FIELDS | p.get("fields", {})
        components = p.get("components", DEFAULT_COMPONENTS)
        rate = float(self.source.fetch.get("rate_limit", {}).get("requests_per_second", 1))
        delay = 1.0 / rate if rate > 0 else 0
        out = workdir / f"estv_{p['tax_year']}.jsonl"
        n_ok = n_fail = 0
        with open(out, "w") as fh:
            for c in self.communes():
                rec = {"commune_bfs": int(c["commune_bfs"]), "name": c["name"]}
                try:
                    hits = self.post("API_searchLocation",
                                     {"Search": c["name"], "Language": 1, "TaxYear": int(p["tax_year"])})
                    time.sleep(delay)
                    hits = hits if isinstance(hits, list) else [hits]
                    match = next((h for h in hits if int(h.get(fields["bfs"], -1)) == rec["commune_bfs"]), None)
                    if match is None and len(hits) == 1:
                        match = hits[0]
                    if match is None:
                        raise RuntimeError("no matching TaxLocationID")
                    loc = match[fields["location_id"]]
                    resp = self.post("API_calculateDetailedTaxes", self.payload(loc, p))
                    time.sleep(delay)
                    total = resp.get(p.get("total_field", "TotalTax"))
                    if total is None:
                        total = sum(float(resp.get(k) or 0) for k in components
                                    if p.get("include_church_tax", False) or "Church" not in k)
                    rec |= {"location_id": loc, "total_tax": float(total), "response": resp}
                    n_ok += 1
                except Exception as e:  # keep going; QA reports missing communes
                    rec |= {"error": str(e)}
                    n_fail += 1
                fh.write(json.dumps(rec) + "\n")
        if n_ok == 0:
            raise RuntimeError(f"ESTV: all {n_fail} commune calculations failed")
        return [(out, self.source.fetch["base_url"])], {"upstream": {"ok": n_ok, "failed": n_fail}}
