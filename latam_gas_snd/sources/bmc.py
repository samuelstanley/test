"""BMC Gestor del Mercado de Gas (Colombia) reports, read from public Power BI dashboards.

The dashboards show a rolling ~12 months, so every pull is archived under
``data/archive/<source>/`` and the history is the union of archived pulls (latest pull
wins; changed values are counted as revisions in the checks).

Each report is normalised to: date, value_raw, unit, value (mcm/d) and its dimension
columns as published (entry point, type, sector, marketer, segment...). Classification
into balance lines happens in ``countries/colombia.py``.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

import pandas as pd
from bs4 import BeautifulSoup

from .. import config, pbi
from ..model import Check, RunContext, SourceData
from ..util import archive_frame, get, load_archive, merge_latest, norm, parse_date

UNIT_RULES = [
    (r"mbtu|mmbtu", "MBTU", config.MBTU_TO_MCM),
    (r"gbtu", "GBTU", config.GBTU_TO_MCM),
    (r"kpc", "KPC", config.KPC_TO_MCM),
    (r"mpc|mmpc|mmcf", "MPC", config.MPC_TO_MCM),
]
_MONTHS = {"ene": 1, "enero": 1, "feb": 2, "febrero": 2, "mar": 3, "marzo": 3, "abr": 4, "abril": 4, "may": 5,
           "mayo": 5, "jun": 6, "junio": 6, "jul": 7, "julio": 7, "ago": 8, "agosto": 8, "sep": 9,
           "septiembre": 9, "set": 9, "oct": 10, "octubre": 10, "nov": 11, "noviembre": 11, "dic": 12,
           "diciembre": 12}


def find_page(source_id: str) -> str:
    meta = config.BMC_REPORTS[source_id]
    if meta.get("url"):
        return meta["url"]
    html = get(config.BMC_INDEX).text
    soup = BeautifulSoup(html, "lxml")
    want = set(norm(meta["title"]).split())
    best, best_score = None, 0.0
    for a in soup.find_all("a", href=True):
        words = set(norm(a.get_text(" ")).split())
        if not words:
            continue
        sc = len(want & words) / len(want)
        if sc > best_score:
            best, best_score = a["href"], sc
    if best is None or best_score < 0.6:
        raise ValueError(f"page for '{meta['title']}' not found on {config.BMC_INDEX}")
    return urljoin(config.BMC_INDEX, best)


def normalise(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Find the date and value columns; everything else is a dimension."""
    notes = []
    cols = list(df.columns)
    date_col = next((c for c in cols if re.search(r"fecha|date|\bdia\b|day", norm(c))
                     and df[c].map(lambda v: isinstance(v, pd.Timestamp) or parse_date(v) is not None).mean() > 0.8),
                    None)
    out = df.copy()
    if date_col is None:
        y = next((c for c in cols if re.search(r"^ano$|^año$|year", norm(c))), None)
        m = next((c for c in cols if re.search(r"^mes$|month", norm(c))), None)
        d = next((c for c in cols if re.search(r"^dia$|^día$|day", norm(c))), None)
        if not (y and m and d):
            raise ValueError(f"no date column among {cols}")
        mon = out[m].map(lambda v: _MONTHS.get(norm(v), None) if not str(v).isdigit() else int(v))
        out["date"] = pd.to_datetime(dict(year=out[y].astype(int), month=mon, day=out[d].astype(int)))
        out = out.drop(columns=[y, m, d])
        notes.append(f"date built from {y}/{m}/{d}")
    else:
        out["date"] = out[date_col].map(lambda v: v if isinstance(v, pd.Timestamp) else parse_date(v))
        if date_col != "date":
            out = out.drop(columns=[date_col])
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    num_cols = [c for c in out.columns if c != "date" and pd.to_numeric(out[c], errors="coerce").notna().mean() > 0.9
                and not re.search(r"codigo|code|\bid\b|nit", norm(c))]
    if not num_cols:
        raise ValueError(f"no numeric measure among {cols}")
    value_col = next((c for c in num_cols if re.search(r"energ|cantidad|volumen|valor|sum|mbtu|gbtu", norm(c))),
                     num_cols[-1])
    unit, factor = "MBTU", config.MBTU_TO_MCM
    for pattern, u, f in UNIT_RULES:
        if re.search(pattern, norm(value_col)):
            unit, factor = u, f
            break
    else:
        notes.append(f"unit not stated in '{value_col}'; MBTU assumed")
    out["value_raw"] = pd.to_numeric(out[value_col], errors="coerce")
    out = out.drop(columns=[value_col])
    out["measure"] = value_col
    out["unit"] = unit
    out["value"] = out["value_raw"] * factor
    dims = [c for c in out.columns if c not in ("date", "value_raw", "value", "unit", "measure")]
    for c in dims:
        out[c] = out[c].astype("string")
    other_nums = [c for c in num_cols if c != value_col]
    if other_nums:
        notes.append(f"other numeric columns kept as dimensions: {other_nums}")
    return out.dropna(subset=["date", "value_raw"]), notes


def fetch_report(ctx: RunContext, source_id: str) -> SourceData:
    meta = config.BMC_REPORTS[source_id]
    page = find_page(source_id)
    views = pbi.report_urls(get(page).text)
    if not views:
        raise ValueError(f"no Power BI embed on {page}")
    errors, df, chosen = [], None, None
    for view in views:
        try:
            rep = pbi.load_report(view)
            vis = pbi.pick(rep, meta.get("keywords", []), config.BMC_PINS.get(source_id))
            df = pbi.run_visual(rep, vis)
            chosen = vis
            break
        except Exception as exc:  # noqa: BLE001 - try the next embed
            errors.append(f"{view[:60]}...: {exc}")
    if df is None or chosen is None:
        raise ValueError("; ".join(errors) or "no visual could be queried")
    norm_df, notes = normalise(df)
    archive_frame(source_id, ctx.run_stamp, norm_df)
    hist = load_archive(source_id)
    dims = [c for c in hist.columns if c not in ("date", "value_raw", "value", "unit", "measure", "run")]
    hist["date"] = pd.to_datetime(hist["date"]).dt.normalize()
    merged, rev = merge_latest(hist, ["date", *dims], "value_raw")
    pinned = source_id in config.BMC_PINS
    checks = [Check("Colombia", f"{source_id}: visual used", "info" if pinned else "warn", detail=(
        f"{chosen.describe()} ({'pinned' if pinned else 'auto-picked; confirm with python -m latam_gas_snd.pbi and pin'})"),
        source=source_id)]
    if not rev.empty:
        checks.append(Check("Colombia", f"{source_id}: values revised between archived pulls", "info", None,
                            float(len(rev)), "latest pull used", source_id))
    neg = merged[merged.value_raw < 0]
    if not neg.empty:
        checks.append(Check("Colombia", f"{source_id}: negative values", "warn", None, float(len(neg)),
                            "kept as published", source_id))
    notes = [f"page {page}", f"visual {chosen.id} on '{chosen.page}'", *notes,
             f"{hist['run'].nunique()} archived pulls"]
    return SourceData(source_id, {"data": merged}, pd.DatetimeIndex(sorted(merged["date"].unique())), notes, checks)


def make_fetcher(source_id: str):
    def _f(ctx: RunContext) -> SourceData:
        return fetch_report(ctx, source_id)
    _f.__name__ = f"fetch_{source_id}"
    return _f

