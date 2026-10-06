"""Kpler LNG imports into Chile (kpler.sdk). Blank if the SDK or credentials are absent.

``Flows.get`` (kpler-sdk 1.0.x): flow_direction=[FlowsDirection.Import],
split=[FlowsSplit.DestinationInstallations], granularity=[FlowsPeriod.Daily],
to_zones=["Chile"], unit=[FlowsMeasurementUnit.CM] -> wide frame, one column per
terminal, cubic metres of LNG. Gas m3 = LNG m3 x 585. If CM is refused, tonnes are
requested and converted at 1,360 m3/t.

These are cargo discharges (import flows), not regas sendout: they are lumpy and
timed by ship arrival. That mismatch is flagged, not smoothed.
"""
from __future__ import annotations

import os

import pandas as pd

from .. import config
from ..model import Check, RunContext, SourceData, SourceUnavailable


def fetch(ctx: RunContext) -> SourceData:
    sid = "kpler"
    email, pwd = os.environ.get(config.KPLER_EMAIL_ENV), os.environ.get(config.KPLER_PASSWORD_ENV)
    if not email or not pwd:
        raise SourceUnavailable(f"{config.KPLER_EMAIL_ENV}/{config.KPLER_PASSWORD_ENV} not set", "no_credentials")
    try:
        from kpler.sdk import FlowsDirection, FlowsMeasurementUnit, FlowsPeriod, FlowsSplit, Platform
        from kpler.sdk.configuration import Configuration
        from kpler.sdk.resources.flows import Flows
    except ImportError as exc:
        raise SourceUnavailable(f"kpler.sdk not installed ({exc}); pip install kpler.sdk", "unavailable") from exc
    flows = Flows(Configuration(Platform.LNG, email, pwd))
    kw = dict(flow_direction=[FlowsDirection.Import], split=[FlowsSplit.DestinationInstallations],
              granularity=[FlowsPeriod.Daily], to_zones=[config.KPLER_CHILE_ZONE],
              start_date=ctx.start.date(), end_date=ctx.end.date())
    unit, factor = "m3 LNG", config.LNG_M3_TO_GAS_M3
    try:
        wide = flows.get(unit=[FlowsMeasurementUnit.CM], **kw)
    except Exception:  # noqa: BLE001 - fall back to tonnes
        wide = flows.get(unit=[FlowsMeasurementUnit.T], **kw)
        unit, factor = "t LNG", config.LNG_TONNE_TO_GAS_M3
    if wide is None or len(wide) == 0:
        raise ValueError("Kpler returned no rows")
    wide = wide.copy()
    date_col = next((c for c in wide.columns if str(c).lower() in ("date", "day", "period")), None)
    if date_col is None and not isinstance(wide.index, pd.DatetimeIndex):
        first = wide.columns[0]
        if pd.to_datetime(wide[first], errors="coerce").notna().mean() > 0.9:
            date_col = first
    if date_col is not None:
        wide = wide.set_index(date_col)
    wide.index = pd.to_datetime(wide.index).normalize()
    long = wide.apply(pd.to_numeric, errors="coerce").stack().rename("value_raw").reset_index()
    long.columns = ["date", "terminal", "value_raw"]
    long["unit"] = unit
    long["value"] = long["value_raw"] * factor / 1e6
    checks = [Check("Chile", "LNG from Kpler is cargo discharge, not regas sendout", "info",
                    detail="daily values are lumpy; residual will show timing mismatch", source=sid)]
    return SourceData(sid, {"data": long}, pd.DatetimeIndex(sorted(long["date"].unique())),
                      [f"unit {unit} x {factor}"], checks)
