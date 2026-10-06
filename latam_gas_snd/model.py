"""Core data structures shared by sources, country builders and the Excel writer."""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Iterable

import numpy as np
import pandas as pd

from . import config

SUPPLY, DEMAND, MEMO = "supply", "demand", "memo"


class SourceUnavailable(Exception):
    """Raised by a fetcher when a source cannot be used (no credentials, no SDK...)."""

    def __init__(self, message: str, status: str = "unavailable"):
        super().__init__(message)
        self.status = status


@dataclass
class SourceData:
    """What a fetcher returns.

    ``tables`` hold long-format frames. The main one is ``tables["data"]`` with at
    least ``date`` (Timestamp, normalised) and ``value`` (mcm/d) columns plus whatever
    attributes the source has (route, point, sector...). ``returned_dates`` are the days
    the source actually returned: an item absent on a returned day is a zero flow, a
    day not returned is blank.
    """

    source_id: str
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    returned_dates: pd.DatetimeIndex = field(default_factory=lambda: pd.DatetimeIndex([]))
    notes: list[str] = field(default_factory=list)
    checks: list["Check"] = field(default_factory=list)

    @property
    def data(self) -> pd.DataFrame:
        return self.tables.get("data", pd.DataFrame(columns=["date", "value"]))


@dataclass
class SourceResult:
    source_id: str
    status: str                     # ok | off | failed | unavailable | no_credentials | empty
    data: SourceData | None
    message: str = ""
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.data is not None


@dataclass
class Line:
    """One column of a country balance."""

    key: str
    label: str
    side: str                        # supply | demand | memo
    sources: tuple[str, ...]         # source ids this line depends on
    series: pd.Series                # mcm/d indexed by daily DatetimeIndex; NaN = blank
    note: str = ""
    frequency: str | None = None     # override of the sources' frequency text
    formula: str | None = None       # derived-in-Excel lines, e.g. "equals_total_supply"

    def source_text(self) -> str:
        if self.formula:
            return "derived in workbook"
        names = [config.SOURCES.get(s, {}).get("name", s) for s in self.sources]
        return "; ".join(names) if names else "no source"

    def frequency_text(self) -> str:
        if self.frequency:
            return self.frequency
        freqs = [config.SOURCES.get(s, {}).get("frequency", "") for s in self.sources]
        return "; ".join(dict.fromkeys(f for f in freqs if f)) or ""


@dataclass
class Check:
    country: str
    check: str
    severity: str = "warn"          # info | warn | error
    date: pd.Timestamp | None = None
    value: float | None = None
    detail: str = ""
    source: str = ""


@dataclass
class CountryBalance:
    country: str
    lines: list[Line]
    index: pd.DatetimeIndex
    residual_label: str = "Residual (supply - demand): fuel, losses, stat diff, missing"
    detail: dict[str, pd.DataFrame] = field(default_factory=dict)   # drill-down sheets
    checks: list[Check] = field(default_factory=list)

    def by_side(self, side: str) -> list[Line]:
        return [ln for ln in self.lines if ln.side == side]

    def residual(self) -> pd.Series:
        """Python mirror of the workbook residual (blank cells count as zero, as in Excel)."""
        sup = _sum_lines(self.by_side(SUPPLY), self.index)
        dem = _sum_lines(self.by_side(DEMAND), self.index, total_supply=sup)
        return sup - dem


def _sum_lines(lines: list[Line], index: pd.DatetimeIndex, total_supply: pd.Series | None = None) -> pd.Series:
    out = pd.Series(0.0, index=index)
    for ln in lines:
        if ln.formula == "equals_total_supply" and total_supply is not None:
            out = out + total_supply
        else:
            out = out + ln.series.reindex(index).fillna(0.0)
    return out


@dataclass
class RunLogEntry:
    when: datetime
    step: str
    status: str
    seconds: float
    days: int | None
    message: str


class RunContext:
    """Holds the run window, switched-off sources, the run log and checks."""

    def __init__(self, start: date, end: date, off: Iterable[str] = (), offline: bool = False):
        self.start = pd.Timestamp(start)
        self.end = pd.Timestamp(end)
        self.off = set(off)
        self.offline = offline
        self.index = pd.date_range(self.start, self.end, freq="D")
        self.log: list[RunLogEntry] = []
        self.checks: list[Check] = []
        self.results: dict[str, SourceResult] = {}
        self.run_stamp = datetime.now().strftime("%Y%m%dT%H%M%S")

    def note(self, step: str, status: str, message: str, seconds: float = 0.0, days: int | None = None):
        self.log.append(RunLogEntry(datetime.now(), step, status, round(seconds, 2), days, message))

    def flag(self, check: Check):
        self.checks.append(check)

    def fetch(self, source_id: str, fn: Callable[["RunContext"], SourceData]) -> SourceResult:
        """Run a fetcher with isolation: any failure leaves that source blank."""
        if source_id in self.off:
            res = SourceResult(source_id, "off", None, "switched off for this run; lines left blank")
            self.note(source_id, "off", res.message)
            self.results[source_id] = res
            return res
        t0 = time.time()
        try:
            sd = fn(self)
            sd.data  # noqa: B018 - ensure attribute exists
            days = len(sd.returned_dates)
            status = "ok" if days else "empty"
            msg = "; ".join(sd.notes) if sd.notes else ""
            if not days:
                msg = ("source returned no days in window; lines left blank. " + msg).strip()
            res = SourceResult(source_id, status, sd if days else None, msg, time.time() - t0)
            for c in sd.checks:
                self.flag(c)
        except SourceUnavailable as exc:
            res = SourceResult(source_id, exc.status, None, f"{exc}; lines left blank", time.time() - t0)
        except Exception as exc:  # noqa: BLE001 - isolate every source
            where = traceback.extract_tb(exc.__traceback__)[-1]
            msg = f"{type(exc).__name__}: {exc}"
            msg = (msg[:300] + "...") if len(msg) > 300 else msg
            res = SourceResult(source_id, "failed", None,
                               f"{msg} [at {where.filename.rsplit('/', 1)[-1]}:{where.lineno}]; lines left blank",
                               time.time() - t0)
        self.results[source_id] = res
        days = len(res.data.returned_dates) if res.data is not None else 0
        self.note(source_id, res.status, res.message, res.seconds, days)
        return res

    def data(self, source_id: str) -> SourceData | None:
        res = self.results.get(source_id)
        return res.data if res is not None and res.ok else None


# --------------------------------------------------------------------------- line helpers
def blank(index: pd.DatetimeIndex) -> pd.Series:
    return pd.Series(np.nan, index=index, dtype=float)


def line_from(sd: SourceData | None, index: pd.DatetimeIndex, mask=None, table: str = "data",
              sign: float = 1.0) -> pd.Series:
    """Sum ``value`` over rows selected by ``mask`` per day.

    Days the source returned but with no selected rows are zero (the item did not
    flow); days the source did not return are blank.
    """
    if sd is None:
        return blank(index)
    df = sd.tables.get(table)
    if df is None or df.empty:
        return blank(index)
    if mask is not None:
        df = df.loc[mask(df) if callable(mask) else mask]
    s = df.groupby("date")["value"].sum(min_count=1)
    s = s.reindex(sd.returned_dates).fillna(0.0) * sign
    return s.reindex(index)


def coverage(series: pd.Series) -> tuple[pd.Timestamp | None, pd.Timestamp | None, int]:
    s = series.dropna()
    if s.empty:
        return None, None, 0
    return s.index.min(), s.index.max(), int(s.shape[0])
