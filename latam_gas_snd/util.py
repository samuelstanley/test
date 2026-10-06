"""HTTP, archive, text, number, date and HTML-table helpers."""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests
from bs4 import BeautifulSoup

from . import config

# --------------------------------------------------------------------------- HTTP
_session: requests.Session | None = None


def session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({"User-Agent": config.USER_AGENT, "Accept-Language": "es,pt;q=0.8,en;q=0.5"})
        _session = s
    return _session


def http(method: str, url: str, *, retries: int | None = None, timeout: int | None = None, **kw) -> requests.Response:
    """Request with retries on network errors and 5xx. 4xx raise immediately."""
    retries = config.HTTP_RETRIES if retries is None else retries
    timeout = config.HTTP_TIMEOUT if timeout is None else timeout
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = session().request(method, url, timeout=timeout, **kw)
            if r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code} from {url}", response=r)
            r.raise_for_status()
            return r
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code < 500:
                raise
            last = exc
        except (requests.ConnectionError, requests.Timeout) as exc:
            last = exc
        if attempt < retries:
            time.sleep(2 ** (attempt + 1))
    assert last is not None
    raise last


def get(url: str, **kw) -> requests.Response:
    return http("GET", url, **kw)


def post(url: str, **kw) -> requests.Response:
    return http("POST", url, **kw)


# --------------------------------------------------------------------------- archive
def archive_path(source_id: str, name: str) -> Path:
    p = config.ARCHIVE_DIR / source_id / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def archive_bytes(source_id: str, name: str, content: bytes) -> Path:
    p = archive_path(source_id, name)
    p.write_bytes(content)
    return p


def archive_frame(source_id: str, run_stamp: str, df: pd.DataFrame, tag: str = "data") -> Path:
    """Save a parsed pull so history can be rebuilt from successive runs."""
    p = archive_path(source_id, f"{tag}_{run_stamp}.parquet")
    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object:
            out[c] = out[c].astype("string")
    out.to_parquet(p, index=False)
    return p


def load_archive(source_id: str, tag: str = "data") -> pd.DataFrame:
    """All archived pulls for a source, oldest first, with a ``run`` column."""
    d = config.ARCHIVE_DIR / source_id
    frames = []
    for p in sorted(d.glob(f"{tag}_*.parquet")) if d.exists() else []:
        df = pd.read_parquet(p)
        df["run"] = p.stem.split("_", 1)[1]
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def merge_latest(df: pd.DataFrame, keys: list[str], value: str = "value") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Latest run wins per key. Returns (merged, revisions) where revisions lists keys
    whose value differed between runs (reported, never averaged)."""
    if df.empty:
        return df, pd.DataFrame()
    df = df.sort_values("run")
    g = df.groupby(keys, dropna=False)[value]
    spread = g.agg(["min", "max", "count"]).reset_index()
    rev = spread[(spread["count"] > 1) & ((spread["max"] - spread["min"]).abs() > 1e-9)]
    merged = df.drop_duplicates(keys, keep="last").drop(columns=["run"])
    return merged.reset_index(drop=True), rev


def cached_download(url: str, source_id: str, refresh: bool = False, **kw) -> bytes:
    """Download once into data/cache/<source>/; refresh=True forces a new download."""
    h = hashlib.sha1(url.encode()).hexdigest()[:12]
    tail = re.sub(r"[^A-Za-z0-9._-]+", "_", url.rsplit("/", 1)[-1])[-80:] or "file"
    p = config.CACHE_DIR / source_id / f"{h}_{tail}"
    if p.exists() and not refresh:
        return p.read_bytes()
    content = get(url, **kw).content
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return content


# --------------------------------------------------------------------------- text
def norm(s: object) -> str:
    """Lower-case, accent-free, single-spaced text for rule matching."""
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ""
    t = unicodedata.normalize("NFKD", str(s))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", t).strip().lower()


def first_match(text: str, rules: Iterable[tuple[str, str]]) -> str | None:
    t = norm(text)
    for pattern, label in rules:
        if re.search(pattern, t):
            return label
    return None


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", norm(s)).strip("_")


# --------------------------------------------------------------------------- numbers
_NUM_CLEAN = re.compile(r"[^\d,.\-+eE]")


def parse_number(s: object, decimal: str = ",") -> float:
    """Parse '1.234,5' (decimal comma) or '1,234.5' (decimal point). Blank -> NaN."""
    if s is None:
        return float("nan")
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip().replace("\xa0", "").replace(" ", "")
    if t in {"", "-", "--", "s/d", "S/D", "nd", "n/d", "N/D"}:
        return float("nan")
    neg = t.startswith("(") and t.endswith(")")
    t = _NUM_CLEAN.sub("", t)
    if not re.search(r"\d", t):
        return float("nan")
    if "," in t and "." in t:
        dec = "," if t.rfind(",") > t.rfind(".") else "."
    elif "," in t:
        dec = "," if decimal == "," or not re.fullmatch(r"-?\d{1,3}(,\d{3})+", t) else "thousands"
    elif "." in t:
        if decimal == "," and re.fullmatch(r"-?\d{1,3}(\.\d{3})+", t):
            dec = "thousands"
        else:
            dec = "."
    else:
        dec = "."
    if dec == ",":
        t = t.replace(".", "").replace(",", ".")
    elif dec == ".":
        t = t.replace(",", "")
    else:  # separator is a thousands separator
        t = t.replace(",", "").replace(".", "")
    try:
        v = float(t)
    except ValueError:
        return float("nan")
    return -v if neg else v


def is_number(s: object) -> bool:
    t = str(s).strip()
    return bool(t) and not re.search(r"[A-Za-z]", t) and parse_number(t) == parse_number(t)


# --------------------------------------------------------------------------- dates
_SPANISH_MONTHS = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8,
    "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12,
    # Portuguese / English extras
    "fev": 2, "mai": 5, "out": 10, "dez": 12, "jan": 1, "apr": 4, "aug": 8, "dec": 12,
}


def parse_date(s: object, default_year: int | None = None) -> pd.Timestamp | None:
    """Parse common LatAm date spellings; day first. Returns None if not a date."""
    if s is None:
        return None
    if isinstance(s, (pd.Timestamp, datetime, date)):
        return pd.Timestamp(s).normalize()
    t = norm(s)
    if not t:
        return None
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ t].*)?", t)
    if m:
        y, mo, d = map(int, m.groups())
        return _mk(y, mo, d)
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", t)
    if m:
        d, mo, y = map(int, m.groups())
        y = y + 2000 if y < 100 else y
        return _mk(y, mo, d)
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})", t)
    if m and default_year:
        d, mo = map(int, m.groups())
        return _mk(default_year, mo, d)
    m = re.fullmatch(r"(\d{1,2})[ /.-]?([a-z]{3})[a-z]*\.?(?:[ /.-]?(\d{2,4}))?", t)
    if m and m.group(2) in _SPANISH_MONTHS:
        d = int(m.group(1))
        mo = _SPANISH_MONTHS[m.group(2)]
        y = m.group(3)
        y = (int(y) + 2000 if int(y) < 100 else int(y)) if y else default_year
        if y:
            return _mk(y, mo, d)
    return None


def _mk(y: int, mo: int, d: int) -> pd.Timestamp | None:
    try:
        return pd.Timestamp(year=y, month=mo, day=d)
    except ValueError:
        return None


# --------------------------------------------------------------------------- HTML tables
def html_tables(html: str | bytes) -> list[list[list[str]]]:
    """Every <table> as a list of rows of cell text, with colspans expanded."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            if tr.find_parent("table") is not table:
                continue
            row: list[str] = []
            for cell in tr.find_all(["td", "th"], recursive=False):
                txt = cell.get_text(" ", strip=True)
                span = int(cell.get("colspan", 1) or 1)
                row.extend([txt] * max(span, 1))
            if row:
                rows.append(row)
        if rows:
            out.append(rows)
    return out


def date_table(rows: list[list[str]], decimal: str = ",") -> tuple[pd.DataFrame, list[str]] | None:
    """Turn rows with a date in the first column into a wide numeric frame.

    Header rows (before the first date row) are joined per column with ' | '.
    Returns (frame indexed by date, column names) or None if no date rows.
    """
    first = next((i for i, r in enumerate(rows) if parse_date(r[0]) is not None), None)
    if first is None:
        return None
    header_rows = rows[:first]
    width = max(len(r) for r in rows[first:])
    names = []
    for j in range(1, width):
        parts = []
        for hr in header_rows:
            if j < len(hr):
                p = hr[j].strip()
                if p and (not parts or parts[-1] != p):
                    parts.append(p)
        names.append(" | ".join(parts) if parts else f"col{j}")
    names = _dedupe(names)
    recs = []
    for r in rows[first:]:
        d = parse_date(r[0])
        if d is None:
            continue
        vals = [parse_number(r[j], decimal) if j < len(r) else float("nan") for j in range(1, width)]
        recs.append([d, *vals])
    df = pd.DataFrame(recs, columns=["date", *names]).set_index("date")
    return df, names


def _dedupe(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for n in names:
        if n in seen:
            seen[n] += 1
            out.append(f"{n} ({seen[n]})")
        else:
            seen[n] = 0
            out.append(n)
    return out


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
