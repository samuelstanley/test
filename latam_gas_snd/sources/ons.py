"""ONS gas-fired generation (Brazil memo), public S3 dataset ``geracao_usina_2_ho``.

Files: one parquet per year up to 2021, one per month from 2022. Values are MWmed per
plant per instant (hourly). Daily MWh = sum over the day's instants x (24 / instants in
that day), which is robust to half-hourly files. Converted to gas equivalent with the
config factors (memo only, never in totals).
"""
from __future__ import annotations

import io
import re

import pandas as pd

from .. import config
from ..model import Check, RunContext, SourceData
from ..util import cached_download, get

COLUMNS = ["din_instante", "nom_tipocombustivel", "nom_usina", "id_estado", "val_geracao"]


def list_keys() -> list[str]:
    keys, token = [], None
    while True:
        params = {"list-type": "2", "prefix": config.ONS_PREFIX, "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        xml = get(config.ONS_BUCKET + "/", params=params).text
        keys += re.findall(r"<Key>([^<]+\.parquet)</Key>", xml)
        m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", xml)
        if not m:
            return keys
        token = m.group(1)


def key_period(key: str) -> tuple[int, int | None] | None:
    m = re.search(r"_(\d{4})(?:_(\d{2}))?\.parquet$", key)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)) if m.group(2) else None


def fetch(ctx: RunContext) -> SourceData:
    sid = "ons"
    keys = list_keys()
    wanted = []
    for k in keys:
        p = key_period(k)
        if p is None:
            continue
        y, mth = p
        first = pd.Timestamp(year=y, month=mth or 1, day=1)
        last = (first + pd.offsets.MonthEnd(1)) if mth else pd.Timestamp(year=y, month=12, day=31)
        if last >= ctx.start and first <= ctx.end:
            wanted.append((k, last))
    if not wanted:
        raise ValueError("no ONS parquet files cover the window")
    frames = []
    recent = ctx.end - pd.Timedelta(days=62)
    for k, last in wanted:
        blob = cached_download(f"{config.ONS_BUCKET}/{k}", sid, refresh=last >= recent)
        df = pd.read_parquet(io.BytesIO(blob), columns=COLUMNS)
        df = df[df["nom_tipocombustivel"].astype(str).isin(config.ONS_GAS_FUELS)].copy()
        # older files store values as decimal strings ('244.33300000'); newer ones as floats
        df["val_geracao"] = pd.to_numeric(df["val_geracao"], errors="coerce")
        df["din_instante"] = pd.to_datetime(df["din_instante"])
        for c in ("nom_usina", "id_estado"):
            df[c] = df[c].astype(str)
        frames.append(df)
    raw = pd.concat(frames, ignore_index=True)
    raw["ts"] = pd.to_datetime(raw["din_instante"])
    raw["date"] = raw["ts"].dt.normalize()
    raw = raw[(raw.date >= ctx.start) & (raw.date <= ctx.end)]
    inst = raw.groupby("date")["ts"].nunique()
    plant = raw.groupby(["date", "nom_usina", "id_estado"], as_index=False)["val_geracao"].sum()
    plant["mwh"] = plant["val_geracao"] * (24 / plant["date"].map(inst))
    plant["value"] = plant["mwh"].map(config.ons_mwh_to_mcm)
    plant = plant.rename(columns={"nom_usina": "plant", "id_estado": "state"})
    checks = []
    short = inst[inst < 24]
    if not short.empty:
        checks.append(Check("Brazil", "ONS days with fewer than 24 instants (scaled to 24h)", "info", None,
                            float(len(short)), f"e.g. {short.index[0].date()} has {short.iloc[0]} instants", sid))
    returned = pd.DatetimeIndex(sorted(plant["date"].unique()))
    return SourceData(sid, {"data": plant[["date", "plant", "state", "mwh", "value"]]}, returned,
                      [f"{len(wanted)} files, fuels {list(config.ONS_GAS_FUELS)}"], checks)
