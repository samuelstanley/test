"""ANP: movimentação de gás natural em gasodutos de transporte (Brazil).

Monthly CSVs (latin-1, ';', decimal comma, wide by day) with daily values by point for
all transporters from Jan 2021, about one month behind. We keep "Volume Realizado" and
"Empacotamento" rows, classify every point (rules in config + overrides CSV), and drop
pipeline-to-pipeline interconnections from both sides (their daily net is a check).

The exact column layout is detected from header names and cell values rather than
assumed, so a renamed column fails loudly in the run log instead of silently.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date
from urllib.parse import urljoin

import pandas as pd
from bs4 import BeautifulSoup

from .. import config
from ..model import Check, RunContext, SourceData
from ..util import cached_download, get, norm, parse_date, parse_number

DAY_RE = re.compile(r"^(?:dia\s*)?0?([1-9]|[12]\d|3[01])$")


# --------------------------------------------------------------------------- listing
def list_files(html: str, page_url: str = config.ANP_PAGE) -> list[tuple[str, str, date | None]]:
    """(title, url, month) of CSV/ZIP links on the ANP page."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"])
        title = a.get_text(" ", strip=True)
        if not re.search(r"\.(csv|zip)(\?|$|/)", href, re.I) and not re.search(r"\bcsv\b", norm(title)):
            continue
        out.append((title, href, file_month(title + " " + href)))
    return list(dict.fromkeys(out))


def file_month(text: str) -> date | None:
    t = norm(text)
    m = re.search(r"(20[12]\d)[-_/ .]?(0[1-9]|1[0-2])(?!\d)", t)
    if m:
        return date(int(m.group(1)), int(m.group(2)), 1)
    months = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6, "jul": 7, "ago": 8, "set": 9,
              "out": 10, "nov": 11, "dez": 12}
    m = re.search(r"\b(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)[a-z]*[-_/ .]*(20[12]\d)", t)
    if m:
        return date(int(m.group(2)), months[m.group(1)], 1)
    return None


# --------------------------------------------------------------------------- parsing
def _find_col(header: list[str], pattern: str, exclude: set[int] = frozenset()) -> int | None:
    for j, h in enumerate(header):
        if j not in exclude and re.search(pattern, norm(h)):
            return j
    return None


def _value_col(rows: list[list[str]], meta: list[int], pattern: str) -> int | None:
    for j in meta:
        vals = {norm(r[j]) for r in rows[:500] if j < len(r)}
        if any(re.search(pattern, v) for v in vals):
            return j
    return None


def unit_factor(text: str) -> float | None:
    t = norm(text).replace("³", "3")
    if re.search(r"mil(hoes|hao)? ?de ?m3|mmm3|mm m3|10\^?6 ?m3|10 6 m3", t) and "mil m3" not in t:
        return 1.0
    if re.search(r"mil m3|mil m 3|10\^?3 ?m3|10 3 m3|1000 m3|km3|mil metros", t):
        return 1 / 1000
    if re.search(r"\bm3\b", t):
        return 1 / 1e6
    return None


def parse_csv(content: bytes, filename: str = "") -> tuple[pd.DataFrame, dict]:
    """Long frame: date, transporter, point, direction_raw, measure, state, pipeline, value_raw.

    Also returns detection info (columns found, unit factor if stated).
    """
    text = content.decode("latin-1")
    if text.startswith("ï»¿"):
        text = content.decode("utf-8-sig")
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=";") if any(c.strip() for c in r)]
    if not rows:
        return pd.DataFrame(), {"error": "empty file"}
    h = None
    for i, r in enumerate(rows[:30]):
        days = [c for c in r if DAY_RE.match(norm(c)) or parse_date(c) is not None]
        if len(days) >= 28 or (any(re.search(r"ponto", norm(c)) for c in r) and len(days) >= 1):
            h = i
            break
    if h is None:
        # long layout: a date column and a value column
        return _parse_long(rows, filename)
    header = rows[h]
    body = [r for r in rows[h + 1:] if len(r) >= len(header) // 2]
    day_cols: dict[int, object] = {}
    for j, c in enumerate(header):
        m = DAY_RE.match(norm(c))
        if m:
            day_cols[j] = int(m.group(1))
        elif parse_date(c) is not None:
            day_cols[j] = parse_date(c)
    meta = [j for j in range(len(header)) if j not in day_cols]
    info: dict = {"layout": "wide", "header": header, "day_columns": len(day_cols)}
    ci = {
        "point": _find_col(header, r"ponto|instalac|local", set(day_cols)),
        "transporter": _find_col(header, r"transport", set(day_cols)),
        "state": _find_col(header, r"^uf$|\buf\b|estado", set(day_cols)),
        "pipeline": _find_col(header, r"gasoduto|duto", set(day_cols)),
        "unit": _find_col(header, r"unidade", set(day_cols)),
        "period": _find_col(header, r"mes|ano|periodo|referencia|competencia|^data", set(day_cols)),
    }
    ci["measure"] = (_find_col(header, r"tipo de volume|grandeza|medida|^volume$|tipo de dado", set(day_cols))
                     or _value_col(body, meta, r"realizad|programad"))
    ci["direction"] = (_value_col(body, [j for j in meta if j != ci["measure"]], r"^receb|^entrega|^entrada|^saida|"
                                  r"^retirada|^injec")
                       or _find_col(header, r"tipo de ponto|natureza|sentido|tipo", set(day_cols) | {ci["measure"]}))
    info["columns"] = {k: (header[v] if v is not None else None) for k, v in ci.items()}
    if ci["point"] is None:
        return pd.DataFrame(), {**info, "error": "no point column"}
    info["unit_factor"] = unit_factor(" ".join(header)) if ci["unit"] is None else None
    fmonth = file_month(filename)
    recs = []
    for r in body:
        def cell(k):
            j = ci[k]
            return r[j].strip() if j is not None and j < len(r) else ""
        meas = cell("measure")
        joined = norm(" ".join(r[j] for j in meta if j < len(r)))
        if "empacotamento" in joined:
            measure = "Empacotamento"
        elif ci["measure"] is None or "realizad" in norm(meas):
            measure = "Volume Realizado"
        else:
            continue
        month = fmonth
        if ci["period"] is not None:
            pv = cell("period")
            pd_ = parse_date(pv) or (parse_date("01/" + pv) if re.fullmatch(r"\d{1,2}/\d{4}", pv) else None)
            if pd_ is not None:
                month = date(pd_.year, pd_.month, 1)
        unit_f = unit_factor(cell("unit")) if ci["unit"] is not None else None
        for j, d in day_cols.items():
            if j >= len(r):
                continue
            v = parse_number(r[j], ",")
            if v != v:  # NaN
                continue
            if isinstance(d, int):
                if month is None:
                    continue
                try:
                    dt = pd.Timestamp(year=month.year, month=month.month, day=d)
                except ValueError:
                    continue
            else:
                dt = d
            recs.append({"date": dt, "transporter": cell("transporter"), "point": cell("point"),
                         "direction_raw": cell("direction"), "measure": measure, "state": cell("state"),
                         "pipeline": cell("pipeline"), "value_raw": v, "unit_factor": unit_f})
    df = pd.DataFrame(recs)
    if df.empty:
        info["error"] = "no Volume Realizado / Empacotamento values parsed"
    return df, info


def _parse_long(rows: list[list[str]], filename: str) -> tuple[pd.DataFrame, dict]:
    header = rows[0]
    jd = _find_col(header, r"^data|dia")
    jv = _find_col(header, r"volume|valor|quantidade")
    if jd is None or jv is None:
        return pd.DataFrame(), {"layout": "unknown", "header": header, "error": "no day columns and no date/value columns"}
    ci = {k: _find_col(header, p) for k, p in {
        "point": r"ponto|instalac", "transporter": r"transport", "state": r"^uf$|estado",
        "pipeline": r"gasoduto", "unit": r"unidade"}.items()}
    meta = [j for j in range(len(header)) if j not in (jd, jv)]
    jm = _value_col(rows[1:], meta, r"realizad|programad")
    jdir = _value_col(rows[1:], [j for j in meta if j != jm], r"^receb|^entrega|^entrada|^saida")
    recs = []
    for r in rows[1:]:
        g = lambda j: r[j].strip() if j is not None and j < len(r) else ""  # noqa: E731
        meas = norm(g(jm))
        joined = norm(" ".join(r))
        if "empacotamento" in joined:
            measure = "Empacotamento"
        elif jm is None or "realizad" in meas:
            measure = "Volume Realizado"
        else:
            continue
        d = parse_date(g(jd))
        v = parse_number(g(jv), ",")
        if d is None or v != v:
            continue
        recs.append({"date": d, "transporter": g(ci["transporter"]), "point": g(ci["point"]),
                     "direction_raw": g(jdir), "measure": measure, "state": g(ci["state"]),
                     "pipeline": g(ci["pipeline"]), "value_raw": v,
                     "unit_factor": unit_factor(g(ci["unit"])) if ci["unit"] is not None else None})
    return pd.DataFrame(recs), {"layout": "long", "header": header}


# --------------------------------------------------------------------------- classification
def load_overrides(path=config.BR_POINT_OVERRIDES) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["point", "transporter", "category", "state", "note"])
    df = pd.read_csv(path, dtype=str, comment="#").fillna("")
    df["key_point"] = df["point"].map(norm)
    df["key_tr"] = df.get("transporter", "").map(norm) if "transporter" in df else ""
    return df


def direction(raw: str) -> str:
    t = norm(raw)
    if re.search(r"receb|entrada|injec|recep", t):
        return "receipt"
    if re.search(r"entrega|saida|retirada|consumo", t):
        return "delivery"
    return "unknown"


def classify(point: str, transporter: str, dirn: str, overrides: pd.DataFrame) -> tuple[str, str]:
    """(category, how) for one point."""
    kp, kt = norm(point), norm(transporter)
    if not overrides.empty:
        o = overrides[(overrides.key_point == kp) & ((overrides.key_tr == "") | (overrides.key_tr == kt))]
        if not o.empty and o.iloc[0]["category"]:
            return o.iloc[0]["category"], "override"
    text = f"{kp} | {kt}"
    for cat, pattern in config.BR_CATEGORIES:
        if re.search(pattern, text):
            if cat in ("uruguaiana", "bolivia_border") and dirn == "delivery":
                return "export", f"rule:{cat}+delivery"
            return cat, f"rule:{cat}"
    return "unclassified", "no rule matched"


# --------------------------------------------------------------------------- fetch
def fetch(ctx: RunContext) -> SourceData:
    sid = "anp"
    html = get(config.ANP_PAGE).text
    files = list_files(html)
    if not files:
        raise ValueError("no CSV/ZIP links found on the ANP page")
    lo = date(ctx.start.year, ctx.start.month, 1)
    hi = date(ctx.end.year, ctx.end.month, 1)
    recent = date(ctx.end.year, ctx.end.month, 1) - pd.DateOffset(months=3)
    frames, notes, checks = [], [], []
    undated = [f for f in files if f[2] is None]
    wanted = [f for f in files if f[2] is not None and lo <= f[2] <= hi] + undated
    for title, url, month in wanted:
        refresh = month is None or pd.Timestamp(month) >= recent
        content = cached_download(url, sid, refresh=refresh)
        for name, blob in _unzip(content, url):
            df, info = parse_csv(blob, name + " " + title)
            if df.empty:
                notes.append(f"{name}: {info.get('error', 'no rows')}")
                continue
            df["file"] = name
            frames.append(df)
    if not frames:
        raise ValueError("no ANP rows parsed; " + "; ".join(notes[:5]))
    pts = pd.concat(frames, ignore_index=True)
    pts["date"] = pd.to_datetime(pts["date"]).dt.normalize()
    pts = pts[(pts.date >= ctx.start) & (pts.date <= ctx.end)]
    # later files win for the same point-day (revisions)
    pts = pts.drop_duplicates(["date", "transporter", "point", "direction_raw", "measure"], keep="last")
    # units
    stated = pts["unit_factor"].dropna()
    if pts["unit_factor"].isna().any():
        rec = pts[(pts.measure == "Volume Realizado") & pts.unit_factor.isna()]
        daily = rec.groupby("date")["value_raw"].sum().median() / 2   # receipts + deliveries ~ 2x flow
        guess = 1 / 1e6 if daily > 1e6 else (1 / 1000 if daily > 1000 else 1.0)
        pts["unit_factor"] = pts["unit_factor"].fillna(guess)
        checks.append(Check("Brazil", "ANP unit inferred from magnitude", "warn", None, guess,
                            f"no unit stated in file; median half-gross daily flow {daily:,.0f} -> factor {guess}",
                            sid))
    elif not stated.empty:
        notes.append(f"unit factor(s) stated in file: {sorted(stated.unique())}")
    pts["value"] = pts["value_raw"] * pts["unit_factor"]
    overrides = load_overrides()
    pts["direction"] = pts["direction_raw"].map(direction)
    keys = pts[["point", "transporter", "direction"]].drop_duplicates()
    cls = {(r.point, r.transporter, r.direction): classify(r.point, r.transporter, r.direction, overrides)
           for r in keys.itertuples()}
    pts["category"] = [cls[(p, t, d)][0] for p, t, d in zip(pts.point, pts.transporter, pts.direction)]
    pts["rule"] = [cls[(p, t, d)][1] for p, t, d in zip(pts.point, pts.transporter, pts.direction)]
    pts.loc[pts.measure == "Empacotamento", "category"] = "linepack"
    if not overrides.empty:
        st = overrides.set_index("key_point")["state"].to_dict()
        miss = pts["state"].eq("") | pts["state"].isna()
        pts.loc[miss, "state"] = pts.loc[miss, "point"].map(lambda p: st.get(norm(p), ""))
    pts["state"] = pts["state"].replace("", "unknown").fillna("unknown")
    # direction for categories that imply it when the file has no direction column
    unknown = pts.direction == "unknown"
    pts.loc[unknown & pts.category.isin(config.BR_RECEIPT_CATEGORIES), "direction"] = "receipt"
    pts.loc[unknown & pts.category.isin(config.BR_DELIVERY_CATEGORIES), "direction"] = "delivery"
    returned = pd.DatetimeIndex(sorted(pts["date"].unique()))
    checks += _checks(pts, sid)
    return SourceData(sid, {"data": pts}, returned, notes, checks)


def _unzip(content: bytes, url: str):
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            for n in z.namelist():
                if n.lower().endswith(".csv"):
                    yield n, z.read(n)
    else:
        yield url.rsplit("/", 1)[-1], content


def _checks(pts: pd.DataFrame, sid: str) -> list[Check]:
    checks = []
    vr = pts[pts.measure == "Volume Realizado"]
    inter = vr[vr.category == "interconnection"]
    if not inter.empty:
        sign = inter["direction"].map({"receipt": 1, "delivery": -1, "unknown": 0})
        net = (inter["value"] * sign).groupby(inter["date"]).sum()
        bad = net[net.abs() > config.BR_TRANSFER_TOLERANCE_MCM]
        checks.append(Check("Brazil", "internal transfers net (interconnections dropped)",
                            "info" if bad.empty else "warn", None, float(len(bad)),
                            f"{len(net)} days; {len(bad)} with |net| > {config.BR_TRANSFER_TOLERANCE_MCM} mcm/d", sid))
        if (inter["direction"] == "unknown").any():
            checks.append(Check("Brazil", "interconnection points without direction", "warn",
                                detail="net transfer check cannot sign these points", source=sid))
    unc = vr[vr.category == "unclassified"].groupby(["point", "transporter", "direction"])["value"].mean()
    for (p, t, d), v in unc.items():
        checks.append(Check("Brazil", "ANP point not classified (add to inputs/br_point_overrides.csv)", "warn",
                            None, float(v), f"{p} | {t} | {d} (mean mcm/d)", sid))
    contra = vr[((vr.category.isin(config.BR_RECEIPT_CATEGORIES)) & (vr.direction == "delivery")) |
                ((vr.category.isin(config.BR_DELIVERY_CATEGORIES)) & (vr.direction == "receipt"))]
    for (p, c, d) in contra[["point", "category", "direction"]].drop_duplicates().itertuples(index=False):
        checks.append(Check("Brazil", "ANP point category contradicts flow direction", "warn",
                            detail=f"{p}: category {c} but direction {d}", source=sid))
    neg = vr[vr.value < 0]
    if not neg.empty:
        checks.append(Check("Brazil", "negative Volume Realizado values", "warn", None, float(len(neg)),
                            "kept as published", sid))
    return checks
