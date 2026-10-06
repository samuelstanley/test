"""Workbook writer.

Country sheet layout (one row per day, units mcm/d)::

    row 1  group       Supply ... | Totals | Demand ... | Totals | Memo (excluded from totals)
    row 2  line label
    row 3  source      (publisher dataset name for every column)
    row 4  frequency
    row 5  note
    row 6  coverage    first - last (days with data)
    row 7+ values      inputs are values; totals, residual and missing count are formulas

Totals, residual (supply - demand) and the missing-inputs count are live Excel formulas;
blank input cells stay blank (they count as zero in the sums and as missing in the count).
"""
from __future__ import annotations

import math
import re
from datetime import datetime

import pandas as pd
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import config
from .model import DEMAND, MEMO, SUPPLY, Check, CountryBalance, Line, RunContext, coverage

HEADER_ROWS = 6
FIRST_DATA_ROW = HEADER_ROWS + 1
NUM_FMT = "#,##0.000"
DATE_FMT = "yyyy-mm-dd"

FILL = {
    SUPPLY: PatternFill("solid", fgColor="DDEBF7"),
    DEMAND: PatternFill("solid", fgColor="FCE4D6"),
    "total": PatternFill("solid", fgColor="E2EFDA"),
    MEMO: PatternFill("solid", fgColor="EDEDED"),
    "head": PatternFill("solid", fgColor="D9D9D9"),
}
BOLD = Font(bold=True)
SMALL = Font(size=8, italic=True, color="555555")
WRAP = Alignment(wrap_text=True, vertical="top")


def safe_sheet(name: str) -> str:
    return re.sub(r"[\[\]:*?/\\]", "-", name)[:31]


def _cell(ws, value, font=None, fill=None, fmt=None, align=None):
    c = WriteOnlyCell(ws, value=value)
    if font:
        c.font = font
    if fill:
        c.fill = fill
    if fmt:
        c.number_format = fmt
    if align:
        c.alignment = align
    return c


def _val(v):
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else v.to_pydatetime()
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "item"):
        return v.item()
    return v


def _cov_text(s: pd.Series) -> str:
    a, b, n = coverage(s)
    return f"{a.date()} to {b.date()} ({n} days)" if n else "no data"


# --------------------------------------------------------------------------- country sheet
def write_balance(wb: Workbook, bal: CountryBalance, title: str | None = None) -> None:
    ws = wb.create_sheet(safe_sheet(title or bal.country))
    sup, dem, memo = bal.by_side(SUPPLY), bal.by_side(DEMAND), bal.by_side(MEMO)
    # column plan
    cols: list[tuple[str, object]] = [("date", None)]
    cols += [("line", ln) for ln in sup] + [("total_supply", None)]
    cols += [("line", ln) for ln in dem] + [("total_demand", None), ("residual", None), ("missing", None)]
    cols += [("line", ln) for ln in memo]
    letter = {i: get_column_letter(i + 1) for i in range(len(cols))}
    s0, s1 = 1, len(sup)
    ts = s1 + 1
    d0, d1 = ts + 1, ts + len(dem)
    td = d1 + 1
    res, miss = td + 1, td + 2

    group, label, src, freq, note, cov = [], [], [], [], [], []
    for kind, ln in cols:
        if kind == "date":
            group.append(_cell(ws, bal.country, BOLD, FILL["head"]))
            label.append(_cell(ws, "Date", BOLD, FILL["head"]))
            src.append(_cell(ws, "Source", SMALL))
            freq.append(_cell(ws, "Frequency", SMALL))
            note.append(_cell(ws, "Note", SMALL))
            cov.append(_cell(ws, "Coverage", SMALL))
            continue
        if kind == "line":
            fill = FILL[ln.side]
            g = {"supply": "Supply", "demand": "Demand", "memo": "Memo (excluded from totals)"}[ln.side]
            group.append(_cell(ws, g, BOLD, fill))
            label.append(_cell(ws, ln.label, BOLD, fill, align=WRAP))
            src.append(_cell(ws, ln.source_text(), SMALL, align=WRAP))
            freq.append(_cell(ws, ln.frequency_text() or "", SMALL, align=WRAP))
            note.append(_cell(ws, ln.note, SMALL, align=WRAP))
            cov.append(_cell(ws, _cov_text(ln.series) if not ln.formula else "formula", SMALL, align=WRAP))
            continue
        text = {"total_supply": "Total supply", "total_demand": "Total demand", "residual": bal.residual_label,
                "missing": "Missing inputs (blank supply/demand cells)"}[kind]
        group.append(_cell(ws, "Totals", BOLD, FILL["total"]))
        label.append(_cell(ws, text, BOLD, FILL["total"], align=WRAP))
        src.append(_cell(ws, "formula", SMALL))
        freq.append(_cell(ws, "daily", SMALL))
        note.append(_cell(ws, "", SMALL))
        cov.append(_cell(ws, "", SMALL))
    for row in (group, label, src, freq, note, cov):
        ws.append(row)
    ws.freeze_panes = f"B{FIRST_DATA_ROW}"
    ws.column_dimensions["A"].width = 11
    for i in range(1, len(cols)):
        ws.column_dimensions[letter[i]].width = 16

    series = {id(ln): ln.series.reindex(bal.index) for _, ln in cols if ln is not None}
    sup_rng = f"{letter[s0]}{{r}}:{letter[s1]}{{r}}" if sup else None
    dem_rng = f"{letter[d0]}{{r}}:{letter[d1]}{{r}}" if dem else None
    for i, day in enumerate(bal.index):
        r = FIRST_DATA_ROW + i
        row = []
        for j, (kind, ln) in enumerate(cols):
            if kind == "date":
                row.append(_cell(ws, day.to_pydatetime(), fmt=DATE_FMT))
            elif kind == "line":
                if ln.formula == "equals_total_supply":
                    v = f'=IF(COUNT({sup_rng.format(r=r)})=0,"",{letter[ts]}{r})' if sup_rng else None
                else:
                    v = _val(series[id(ln)].iloc[i])
                row.append(_cell(ws, v, fmt=NUM_FMT))
            elif kind == "total_supply":
                row.append(_cell(ws, f"=SUM({sup_rng.format(r=r)})" if sup_rng else 0, BOLD, fmt=NUM_FMT))
            elif kind == "total_demand":
                row.append(_cell(ws, f"=SUM({dem_rng.format(r=r)})" if dem_rng else 0, BOLD, fmt=NUM_FMT))
            elif kind == "residual":
                row.append(_cell(ws, f"={letter[ts]}{r}-{letter[td]}{r}", BOLD, fmt=NUM_FMT))
            elif kind == "missing":
                parts = [f"COUNTBLANK({rng.format(r=r)})" for rng in (sup_rng, dem_rng) if rng]
                row.append(_cell(ws, "=" + "+".join(parts) if parts else 0, fmt="0"))
        ws.append(row)


# --------------------------------------------------------------------------- generic tables
def write_wide(wb: Workbook, name: str, df: pd.DataFrame, title: str, source: str, index_label: str = "Date") -> None:
    ws = wb.create_sheet(safe_sheet(name))
    ws.append([_cell(ws, title, BOLD)])
    ws.append([_cell(ws, source, SMALL)])
    is_dates = isinstance(df.index, pd.DatetimeIndex)
    ws.append([_cell(ws, index_label if is_dates else "", BOLD, FILL["head"])] +
              [_cell(ws, str(c), BOLD, FILL["head"], align=WRAP) for c in df.columns])
    ws.freeze_panes = "B4"
    ws.column_dimensions["A"].width = 11
    for d, vals in zip(df.index, df.itertuples(index=False)):
        first = _cell(ws, d.to_pydatetime(), fmt=DATE_FMT) if is_dates else _cell(ws, _val(d))
        ws.append([first] + [_cell(ws, _val(v), fmt=NUM_FMT if isinstance(_val(v), float) else None) for v in vals])


def write_records(wb: Workbook, name: str, df: pd.DataFrame, title: str | None = None, widths: dict | None = None) -> None:
    ws = wb.create_sheet(safe_sheet(name))
    if title:
        ws.append([_cell(ws, title, BOLD)])
    ws.append([_cell(ws, str(c), BOLD, FILL["head"]) for c in df.columns])
    for i, c in enumerate(df.columns):
        ws.column_dimensions[get_column_letter(i + 1)].width = (widths or {}).get(c, 18)
    ws.freeze_panes = "A3" if title else "A2"
    for vals in df.itertuples(index=False):
        row = []
        for v in vals:
            v = _val(v)
            if isinstance(v, datetime):
                row.append(_cell(ws, v, fmt=DATE_FMT))
            elif isinstance(v, (list, dict, tuple)):
                row.append(_cell(ws, str(v)))
            else:
                row.append(_cell(ws, v))
        ws.append(row)


def checks_frame(checks: list[Check]) -> pd.DataFrame:
    sev = {"error": 0, "warn": 1, "info": 2}
    rows = [{"Country": c.country, "Severity": c.severity, "Check": c.check, "Date": c.date, "Value": c.value,
             "Detail": c.detail, "Source": c.source} for c in checks]
    df = pd.DataFrame(rows, columns=["Country", "Severity", "Check", "Date", "Value", "Detail", "Source"])
    if df.empty:
        return df
    df["_s"] = df.Severity.map(sev).fillna(3)
    return df.sort_values(["Country", "_s", "Check", "Date"], na_position="first").drop(columns="_s")


def sources_frame(ctx: RunContext) -> pd.DataFrame:
    rows = []
    for sid, meta in config.SOURCES.items():
        res = ctx.results.get(sid)
        dates = res.data.returned_dates if res is not None and res.data is not None else pd.DatetimeIndex([])
        rows.append({"Source id": sid, "Dataset": meta["name"], "Publisher": meta["publisher"], "URL": meta["url"],
                     "Frequency": meta["frequency"], "Unit / conversion": meta["unit"],
                     "Status this run": res.status if res else "not run",
                     "First date": dates.min() if len(dates) else None,
                     "Last date": dates.max() if len(dates) else None, "Days with data": len(dates),
                     "Notes": meta["notes"], "Run message": res.message if res else ""})
    return pd.DataFrame(rows)


def coverage_frames(ctx: RunContext, bals: dict[str, CountryBalance]) -> tuple[pd.DataFrame, pd.DataFrame]:
    src = sources_frame(ctx)[["Source id", "Dataset", "Status this run", "First date", "Last date", "Days with data"]]
    window = len(ctx.index)
    src["Days in window"] = window
    src["Share of window"] = src["Days with data"] / window
    rows = []
    for name, bal in bals.items():
        for ln in bal.lines:
            a, b, n = coverage(ln.series)
            rows.append({"Country": name, "Line": ln.label, "Side": ln.side, "Sources": ", ".join(ln.sources) or "none",
                         "First date": a, "Last date": b, "Days with data": n, "Share of window": n / window,
                         "Note": "formula in workbook" if ln.formula else ""})
    return src, pd.DataFrame(rows)


def runlog_frame(ctx: RunContext, extra: list[tuple[str, str]] = ()) -> pd.DataFrame:
    head = [{"When": datetime.now(), "Step": "parameters", "Status": "",
             "Seconds": None, "Days returned": None,
             "Message": f"window {ctx.start.date()} to {ctx.end.date()}; sources off: "
                        f"{', '.join(sorted(ctx.off)) or 'none'}; run {ctx.run_stamp}"}]
    rows = head + [{"When": e.when, "Step": e.step, "Status": e.status, "Seconds": e.seconds, "Days returned": e.days,
                    "Message": e.message} for e in ctx.log]
    rows += [{"When": datetime.now(), "Step": k, "Status": "", "Seconds": None, "Days returned": None, "Message": v}
             for k, v in extra]
    return pd.DataFrame(rows)


ABOUT = [
    ("What", "Daily natural gas supply/demand balance from historical public data, mcm/d."),
    ("Countries", "Argentina, Brazil, Colombia, Bolivia, Chile, Uruguay."),
    ("Layout", "Rows 1-6 of each country sheet: group, line, source, frequency, note, coverage. Data from row 7."),
    ("Formulas", "Total supply, Total demand, Residual (supply - demand) and Missing inputs are live formulas; "
                 "every other cell is a value fetched from a source."),
    ("Blanks", "A blank cell means the source returned nothing for that day (or is off / failed / not public). "
               "Blanks are never filled, plugged or back-solved; the residual shows the gap."),
    ("Residual", "Fuel, losses, statistical difference and anything missing. For Chile it also holds power, "
                 "distribution and industry demand (no daily public source)."),
    ("Memo", "Memo columns are cross-checks and are excluded from totals."),
    ("Checks", "Inconsistencies, unverifiable figures, timing mismatches and negative derived values are listed in "
               "Checks; nothing is adjusted away."),
    ("Provenance", "Sources sheet: dataset, URL, unit and conversion, status this run. Coverage: first date, last date "
                   "and days with data per source and per line. Run log: what each fetch did."),
]


def write_main(path, ctx: RunContext, bals: dict[str, CountryBalance], flows: list[Line], extra_log=()) -> None:
    wb = Workbook(write_only=True)
    write_records(wb, "About", pd.DataFrame(ABOUT, columns=["Topic", "Detail"]), widths={"Topic": 14, "Detail": 120})
    for name in ("Argentina", "Brazil", "Colombia", "Bolivia", "Chile", "Uruguay"):
        write_balance(wb, bals[name])
    write_flows(wb, ctx, flows)
    all_checks = ctx.checks + [c for b in bals.values() for c in b.checks]
    write_records(wb, "Checks", checks_frame(all_checks), widths={"Check": 50, "Detail": 90})
    write_records(wb, "Sources", sources_frame(ctx), widths={"Dataset": 45, "URL": 60, "Notes": 60, "Run message": 80})
    src, lines = coverage_frames(ctx, bals)
    write_records(wb, "Coverage", src, "Coverage by source (what each source actually returned)",
                  widths={"Dataset": 50})
    write_records(wb, "Coverage by line", lines, "Coverage by balance line", widths={"Line": 55, "Sources": 40})
    write_records(wb, "Run log", runlog_frame(ctx, extra_log), widths={"Message": 120, "Step": 24})
    wb.save(path)


def write_flows(wb: Workbook, ctx: RunContext, flows: list[Line]) -> None:
    bal = CountryBalance("Flows by route", flows, ctx.index, residual_label="")
    ws = wb.create_sheet("Flows by route")
    rows = [["Cross-border flows"], ["Route (from -> to)"], ["Source"], ["Frequency"], ["Note"], ["Coverage"]]
    for ln in flows:
        rows[0].append("Pipeline / LNG flow")
        rows[1].append(ln.label)
        rows[2].append(ln.source_text())
        rows[3].append(ln.frequency_text())
        rows[4].append(ln.note)
        rows[5].append(_cov_text(ln.series))
    for i, r in enumerate(rows):
        ws.append([_cell(ws, v, BOLD if i < 2 else SMALL, FILL["head"] if i < 2 else None, align=WRAP) for v in r])
    ws.freeze_panes = f"B{FIRST_DATA_ROW}"
    for j in range(1, len(flows) + 1):
        ws.column_dimensions[get_column_letter(j + 1)].width = 18
    vals = [ln.series.reindex(bal.index) for ln in flows]
    for i, day in enumerate(bal.index):
        ws.append([_cell(ws, day.to_pydatetime(), fmt=DATE_FMT)] + [_cell(ws, _val(v.iloc[i]), fmt=NUM_FMT) for v in vals])


def write_drilldown(path, ctx: RunContext, bal: CountryBalance, extra_checks: list[Check] = ()) -> None:
    wb = Workbook(write_only=True)
    write_balance(wb, bal, "Balance")
    src_names = sorted({s for ln in bal.lines for s in ln.sources})
    for name, df in bal.detail.items():
        if isinstance(df.index, pd.DatetimeIndex):
            write_wide(wb, name, df, f"{bal.country}: {name} (mcm/d)", "Sources: " + ", ".join(
                config.SOURCES[s]["name"] for s in src_names if s in config.SOURCES))
        else:
            write_records(wb, name, df)
    checks = [c for c in ctx.checks if c.country == bal.country] + bal.checks + list(extra_checks)
    write_records(wb, f"{bal.country[:2].upper()} checks", checks_frame(checks), widths={"Check": 50, "Detail": 90})
    s = sources_frame(ctx)
    write_records(wb, "Sources", s[s["Source id"].isin(src_names)], widths={"Dataset": 45, "URL": 60, "Run message": 80})
    wb.save(path)
