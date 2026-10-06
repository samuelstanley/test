"""XM API (Colombia): ConsCombustibleMBTU, declared thermal fuel burn by plant.

POST {XM_API}/daily (falls back to /hourly) with
``{"MetricId": "ConsCombustibleMBTU", "Entity": "Recurso", "StartDate", "EndDate"}`` in
30-day chunks. The response's entity records are flattened as published; the fuel
field is found by name ("combustible"/"fuel") and only gas rows are kept. If no fuel
field exists the burn cannot be attributed to gas, and the line is memo-only with a
flag. Values are MBTU (million Btu) -> mcm/d via GBTU x 0.0283.
"""
from __future__ import annotations

import re

import pandas as pd

from .. import config
from ..model import Check, RunContext, SourceData
from ..util import norm, post

GAS_FUEL = r"\bgas\b|\bgn\b|gas natural|gnl|gni|gas importado"


def _records(payload: dict) -> list[dict]:
    out = []
    for item in payload.get("Items", []) or []:
        d = item.get("Date")
        for key in ("DailyEntities", "HourlyEntities", "Entities"):
            for ent in item.get(key, []) or []:
                rec = {"Date": d}
                vals = ent.get("Values") if isinstance(ent.get("Values"), dict) else {}
                rec.update({k: v for k, v in ent.items() if k != "Values"})
                rec.update(vals)
                out.append(rec)
    return out


def _value(rec: dict) -> float | None:
    hours = [v for k, v in rec.items() if re.fullmatch(r"(?i)hour\d{2}", k)]
    if hours:
        nums = [float(v) for v in hours if v not in (None, "")]
        return sum(nums) if nums else None
    v = rec.get("Value", rec.get("value"))
    return None if v in (None, "") else float(v)


def fetch(ctx: RunContext) -> SourceData:
    sid = "xm"
    recs, endpoint = [], None
    d0 = ctx.start
    while d0 <= ctx.end:
        d1 = min(d0 + pd.Timedelta(days=config.XM_CHUNK_DAYS - 1), ctx.end)
        body = {"MetricId": config.XM_METRIC, "StartDate": d0.strftime("%Y-%m-%d"),
                "EndDate": d1.strftime("%Y-%m-%d"), "Entity": config.XM_ENTITY, "Filter": []}
        for ep in ([endpoint] if endpoint else ["daily", "hourly"]):
            r = post(f"{config.XM_API}/{ep}", json=body)
            got = _records(r.json())
            if got or endpoint:
                endpoint = ep
                recs += got
                break
        d0 = d1 + pd.Timedelta(days=1)
    if not recs:
        raise ValueError(f"XM returned no records for {config.XM_METRIC}/{config.XM_ENTITY}")
    df = pd.DataFrame(recs)
    df["date"] = pd.to_datetime(df["Date"]).dt.normalize()
    df["value_mbtu"] = [_value(r) for r in recs]
    fuel_col = next((c for c in df.columns if re.search(r"combust|fuel", norm(c))), None)
    plant_col = next((c for c in df.columns if norm(c) in ("code", "codigo", "recurso", "name", "id")), None)
    df["plant"] = df[plant_col].astype(str) if plant_col else "all"
    checks = []
    if fuel_col:
        df["fuel"] = df[fuel_col].astype(str)
        df["is_gas"] = df["fuel"].map(lambda f: bool(re.search(GAS_FUEL, norm(f))))
    else:
        df["fuel"] = "(not published)"
        df["is_gas"] = False
        checks.append(Check("Colombia", "XM fuel field not found", "warn",
                            detail=f"columns {list(df.columns)}; burn not attributable to gas, memo only", source=sid))
    df["value"] = df["value_mbtu"] * config.MBTU_TO_MCM
    out = df[["date", "plant", "fuel", "is_gas", "value_mbtu", "value"]].dropna(subset=["value"])
    return SourceData(sid, {"data": out}, pd.DatetimeIndex(sorted(out["date"].unique())),
                      [f"endpoint /{endpoint}, fuel field {fuel_col!r}, plant field {plant_col!r}"], checks)
