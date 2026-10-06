"""ENARGAS (ENReGE) daily operating data.

Partes (imports / exports by route)
    ``dod-partes-exp-imp-consulta.php?tipo=importaciones|exp_dentro|exp_fuera``.
    The page has no <form>; its date boxes are driven by script, so a plain GET only
    returns the current-year default view. ``_discover`` scans the page's inputs and
    scripts for parameter names and endpoints, then tries name pairs x date formats x
    methods until a query returns exactly the requested (historic) dates. The winner
    is cached in ``data/enargas_query.json`` and re-verified each run. If discovery
    fails the default view is used and the run log says so (coverage will show it).

Charts (``dod-graficos-de-programacion-items.php?cat=6`` injection, ``cat=8`` demand,
linepack on ``dod-graficos-de-programacion.php``) are vector PDFs read by
:mod:`chartpdf`. Each pull is archived; history is the union of archived pulls with the
latest pull winning, and any day whose value changed between pulls is reported.
"""
from __future__ import annotations

import io
import re
import time
from datetime import date
from urllib.parse import urljoin, urlencode

import pandas as pd
from bs4 import BeautifulSoup

from .. import config
from ..model import Check, RunContext, SourceData, SourceUnavailable
from ..util import (archive_bytes, archive_frame, date_table, first_match, get, html_tables, http,
                    load_archive, merge_latest, norm, parse_date, read_json, slug, write_json)
from .chartpdf import parse_chart_pdf

TIPOS = {
    "enargas_imports": "importaciones",
    "enargas_exp_dentro": "exp_dentro",
    "enargas_exp_fuera": "exp_fuera",
}

DEFAULT_PAIRS = [
    ("fecha_desde", "fecha_hasta"), ("desde", "hasta"), ("fechaDesde", "fechaHasta"),
    ("fecha_inicio", "fecha_fin"), ("fdesde", "fhasta"), ("f_desde", "f_hasta"), ("inicio", "fin"),
    ("fechadesde", "fechahasta"), ("fechaInicio", "fechaFin"), ("fecha1", "fecha2"),
    ("from", "to"), ("date_from", "date_to"), ("fd", "fh"), ("fini", "ffin"),
]
DATE_FORMATS = ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"]
PROBE_WINDOW = (date(2021, 3, 1), date(2021, 3, 10))   # outside any current-year default view
MAX_PROBES = 160


# =========================================================================== partes
def parse_partes(content: bytes | str) -> pd.DataFrame:
    """Wide frame: index date, one column per route plus (if present) 'Total'."""
    if isinstance(content, bytes):
        if content[:4] == b"\xd0\xcf\x11\xe0" or content[:2] == b"PK":
            return _parse_excel(content)
        text = content.decode("utf-8", errors="replace")
        if "�" in text:
            text = content.decode("latin-1")
    else:
        text = content
    best = None
    for rows in html_tables(text):
        res = date_table(rows, decimal=",")
        if res is not None and (best is None or len(res[0]) > len(best)):
            best = res[0]
    if best is None:
        return pd.DataFrame()
    best = best.loc[:, [c for c in best.columns if best[c].notna().any()]]
    return best[~best.index.duplicated(keep="last")].sort_index()


def _parse_excel(content: bytes) -> pd.DataFrame:
    raw = pd.read_excel(io.BytesIO(content), header=None, dtype=str)
    rows = [[("" if pd.isna(v) else str(v)) for v in r] for r in raw.itertuples(index=False)]
    res = date_table(rows, decimal=",")
    return res[0] if res else pd.DataFrame()


def _total_col(df: pd.DataFrame) -> str | None:
    tots = [c for c in df.columns if re.search(r"\btotal\b", norm(c))]
    return tots[-1] if tots else None


def _discover_candidates(html: str, page_url: str) -> tuple[list[tuple[str, str]], list[str], dict]:
    soup = BeautifulSoup(html, "lxml")
    names: set[str] = set()
    extra: dict[str, str] = {}
    for el in soup.find_all(["input", "select"]):
        for attr in ("name", "id"):
            if el.get(attr):
                names.add(el[attr])
        if el.name == "input" and (el.get("type") or "").lower() == "hidden" and el.get("name"):
            extra[el["name"]] = el.get("value", "")
    scripts = " ".join(s.get_text(" ") for s in soup.find_all("script"))
    names.update(re.findall(r"""['"#]([A-Za-z_][\w-]*(?:desde|hasta|inicio|fin|from|to|fecha)[\w-]*)['"]""",
                            scripts, flags=re.I))
    names.update(re.findall(r"[?&]([A-Za-z_]\w*)=", scripts))
    names.update(re.findall(r"""\b(?:name|id)\s*[:=]\s*['"]([A-Za-z_][\w-]*)['"]""", scripts))
    endpoints = [page_url]
    for u in re.findall(r"""['"]([\w./?=&%-]*\.php[^'"\s]*)['"]""", scripts):
        endpoints.append(urljoin(page_url, u.split("?")[0]))
    for a in soup.find_all("a", href=True):
        t = norm(a.get_text(" ")) + " " + norm(a["href"])
        if re.search(r"xls|descargar|exportar|excel", t):
            endpoints.append(urljoin(page_url, a["href"].split("?")[0]))
    froms = [n for n in names if re.search(r"desde|inicio|from|fecha1|ini\b|_ini|fd$", n, re.I)]
    tos = [n for n in names if re.search(r"hasta|fin\b|_fin|to$|fecha2|fh$", n, re.I)]

    def affinity(f, t):
        common = 0
        for a, b in zip(f.lower(), t.lower()):
            if a != b:
                break
            common += 1
        return -common
    pairs = sorted({(f, t) for f in froms for t in tos if f != t}, key=lambda p: affinity(*p))
    pairs += [p for p in DEFAULT_PAIRS if p not in pairs]
    ep = list(dict.fromkeys(endpoints))
    return pairs, ep, extra


def _query(recipe: dict, tipo: str, d0: date, d1: date) -> bytes:
    fmt = recipe["fmt"]
    params = {**recipe.get("extra", {}), recipe.get("tipo_param", "tipo"): tipo,
              recipe["from"]: d0.strftime(fmt), recipe["to"]: d1.strftime(fmt)}
    if recipe["method"] == "GET":
        r = http("GET", recipe["url"], params=params, retries=1)
    else:
        r = http("POST", recipe["url"], params={"tipo": tipo}, data=params, retries=1)
    return r.content


def _answers(content: bytes, d0: date, d1: date) -> bool:
    df = parse_partes(content)
    if df.empty:
        return False
    idx = df.index
    lo, hi = pd.Timestamp(d0), pd.Timestamp(d1)
    inside = ((idx >= lo) & (idx <= hi)).sum()
    return inside > 0 and inside >= 0.9 * len(idx)


def _discover(tipo: str, ctx: RunContext) -> dict | None:
    cached = read_json(config.ENARGAS_QUERY_CACHE)
    d0, d1 = PROBE_WINDOW
    if cached:
        try:
            if _answers(_query(cached, tipo, d0, d1), d0, d1):
                return cached
            ctx.note("enargas_discover", "warn", "cached ENARGAS query recipe no longer returns the requested dates; rediscovering")
        except Exception as exc:  # noqa: BLE001
            ctx.note("enargas_discover", "warn", f"cached ENARGAS recipe failed ({exc}); rediscovering")
    page_url = config.ENARGAS_PARTES
    html = get(page_url, params={"tipo": tipo}).text
    pairs, endpoints, extra = _discover_candidates(html, page_url)
    probes = 0
    for url in endpoints[:4]:
        for frm, to in pairs:
            for fmt in DATE_FORMATS:
                for method in ("GET", "POST"):
                    if probes >= MAX_PROBES:
                        return None
                    probes += 1
                    recipe = {"url": url, "method": method, "from": frm, "to": to, "fmt": fmt,
                              "extra": extra, "tipo_param": "tipo"}
                    try:
                        ok = _answers(_query(recipe, tipo, d0, d1), d0, d1)
                    except Exception:  # noqa: BLE001 - a failed probe is just a miss
                        ok = False
                    if ok:
                        recipe["verified"] = pd.Timestamp.now().isoformat()
                        recipe["probes"] = probes
                        write_json(config.ENARGAS_QUERY_CACHE, recipe)
                        ctx.note("enargas_discover", "ok", f"date-range query found after {probes} probes: "
                                 f"{method} {url} {frm}/{to} {fmt}")
                        return recipe
                    time.sleep(0.2)
    ctx.note("enargas_discover", "warn", f"no date-range query found after {probes} probes; "
             "only the default (current-year) view is available. Read the page JS (Fecha Desde/Hasta, "
             "'Descargar .xls') and hard-code the request in data/enargas_query.json")
    return None


def fetch_partes(ctx: RunContext, source_id: str) -> SourceData:
    tipo = TIPOS[source_id]
    recipe = _discover(tipo, ctx)
    frames = []
    notes = []
    if recipe:
        start = max(ctx.start.date(), config.START_DATE)
        y = start.year
        while y <= ctx.end.year:
            d0 = max(start, date(y, 1, 1))
            d1 = min(ctx.end.date(), date(y, 12, 31))
            content = _query(recipe, tipo, d0, d1)
            archive_bytes(source_id, f"raw_{ctx.run_stamp}_{y}.html", content)
            frames.append(parse_partes(content))
            y += 1
    else:
        content = get(config.ENARGAS_PARTES, params={"tipo": tipo}).content
        archive_bytes(source_id, f"raw_{ctx.run_stamp}_default.html", content)
        frames.append(parse_partes(content))
        notes.append("date-range query not found: default view only")
    wide = pd.concat([f for f in frames if not f.empty]) if frames else pd.DataFrame()
    if wide.empty:
        raise ValueError("no table with dates found in ENARGAS partes response")
    wide = wide[~wide.index.duplicated(keep="last")].sort_index()
    long = _partes_long(wide)
    archive_frame(source_id, ctx.run_stamp, long)
    hist = load_archive(source_id)
    merged, rev = merge_latest(hist, ["date", "route"], "value_km3")
    return _partes_source_data(source_id, merged, rev, notes)


def _partes_long(wide: pd.DataFrame) -> pd.DataFrame:
    tot = _total_col(wide)
    recs = []
    for d, row in wide.iterrows():
        for c in wide.columns:
            v = row[c]
            if pd.isna(v):
                continue
            recs.append({"date": d, "route": c, "is_total": c == tot, "value_km3": float(v)})
    return pd.DataFrame(recs)


def _partes_source_data(source_id: str, long: pd.DataFrame, revisions: pd.DataFrame, notes: list[str]) -> SourceData:
    tipo = TIPOS[source_id]
    rules = config.ENARGAS_IMPORT_ROUTES if tipo == "importaciones" else config.ENARGAS_EXPORT_ROUTES
    long = long.copy()
    long["date"] = pd.to_datetime(long["date"]).dt.normalize()
    long["is_total"] = long["is_total"].astype(bool)
    totals = long[long.is_total].rename(columns={"value_km3": "total_km3"})[["date", "total_km3"]].copy()
    totals["value"] = totals["total_km3"] * config.ENARGAS_KM3_TO_MCM
    routes = long[~long.is_total].copy()
    routes["counterparty"] = [first_match(r, rules) or "Unclassified" for r in routes["route"]]
    routes["methanex"] = routes["route"].map(lambda r: bool(re.search(config.ENARGAS_METHANEX, norm(r))))
    routes["value"] = routes["value_km3"] * config.ENARGAS_KM3_TO_MCM
    checks: list[Check] = []
    country = "Argentina"
    # route totals vs ENARGAS Total column (acceptance check)
    if not totals.empty:
        s = routes.groupby("date")["value_km3"].sum()
        t = totals.set_index("date")["total_km3"]
        diff = (s.reindex(t.index).fillna(0) - t)
        bad = diff[diff.abs() > config.PARTES_TOTAL_TOLERANCE_KM3]
        checks.append(Check(country, f"{tipo}: parsed routes vs ENARGAS Total", "info" if bad.empty else "error",
                            None, float(len(bad)),
                            f"{len(t)} days compared, {len(bad)} differ by more than "
                            f"{config.PARTES_TOTAL_TOLERANCE_KM3} thousand m3", source_id))
        for d, v in bad.items():
            checks.append(Check(country, f"{tipo}: routes sum != Total", "error", d, float(v),
                                "route sum minus Total, thousand m3", source_id))
    else:
        checks.append(Check(country, f"{tipo}: no Total column found", "warn", detail="routes could not be "
                            "reconciled to an ENARGAS total", source=source_id))
    for r in sorted(routes.loc[routes.counterparty == "Unclassified", "route"].unique()):
        checks.append(Check(country, f"{tipo}: route not mapped to a counterparty", "warn", detail=r,
                            source=source_id))
    neg = routes[routes.value_km3 < 0]
    for _, r in neg.iterrows():
        checks.append(Check(country, f"{tipo}: negative route value", "warn", r["date"], r["value"],
                            r["route"], source_id))
    if not revisions.empty:
        checks.append(Check(country, f"{tipo}: values revised between archived pulls", "info", None,
                            float(len(revisions)), "latest pull used; earlier values kept in data/archive",
                            source_id))
    returned = pd.DatetimeIndex(sorted(set(routes["date"]) | set(totals["date"])))
    sd = SourceData(source_id, {"data": routes, "totals": totals, "revisions": revisions}, returned, notes, checks)
    return sd


def fetch_imports(ctx):
    return fetch_partes(ctx, "enargas_imports")


def fetch_exp_dentro(ctx):
    return fetch_partes(ctx, "enargas_exp_dentro")


def fetch_exp_fuera(ctx):
    return fetch_partes(ctx, "enargas_exp_fuera")


# =========================================================================== charts
def chart_links(page_url: str, params: dict | None = None, follow: bool = True) -> list[tuple[str, str]]:
    """(title, url) of PDF/image chart links on a page, following item pages one level."""
    html = get(page_url, params=params).text
    full = page_url + ("?" + urlencode(params) if params else "")
    soup = BeautifulSoup(html, "lxml")
    out, subpages = [], []
    for a in soup.find_all("a", href=True):
        href = urljoin(full, a["href"])
        title = a.get_text(" ", strip=True) or a.get("title") or href.rsplit("/", 1)[-1]
        if re.search(r"\.(pdf|png|jpe?g|gif|svg)(\?|$)", href, re.I):
            out.append((title, href))
        elif follow and re.search(r"item|grafic|id=", href, re.I) and href != full:
            subpages.append(href)
    for img in soup.find_all(["img", "embed", "iframe", "object"]):
        src = img.get("src") or img.get("data")
        if src and re.search(r"\.(pdf|png|jpe?g|svg)", src, re.I):
            out.append((img.get("alt") or img.get("title") or src.rsplit("/", 1)[-1], urljoin(full, src)))
    if not out and follow:
        for sp in list(dict.fromkeys(subpages))[:30]:
            try:
                out.extend(chart_links(sp, None, follow=False))
            except Exception:  # noqa: BLE001
                continue
    return list(dict.fromkeys(out))


def _chart_identity(title: str) -> str:
    return re.sub(r"\d+", "", slug(title)).strip("_")


def _parse_and_archive(source_id: str, ctx: RunContext, links: list[tuple[str, str]]) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    data, legends, warns = [], [], []
    today = pd.Timestamp.today().normalize()
    for title, url in links:
        if not url.lower().split("?")[0].endswith(".pdf"):
            warns.append(f"{title}: not a PDF ({url.rsplit('/', 1)[-1]}); image charts are not parsed")
            continue
        content = get(url).content
        stem = f"{ctx.run_stamp}_{slug(title)[:60]}"
        archive_bytes(source_id, stem + ".pdf", content)
        res = parse_chart_pdf(content)
        if not res.data.empty and res.data["date"].max() > today + pd.Timedelta(days=7):
            res.data["date"] = res.data["date"] - pd.DateOffset(years=1)
            res.warnings.append("dates without year fell in the future; shifted back one year")
        ident = _chart_identity(res.title or title)
        unit = res.unit or "thousand m3"
        factor = 1.0 if unit == "mcm" else config.ENARGAS_KM3_TO_MCM
        d = res.data.copy()
        d["chart"] = ident
        d["chart_title"] = res.title or title
        d["unit"] = unit
        d["value_raw"] = d["value"]
        d["value"] = d["value"] * factor
        data.append(d)
        leg = res.legend.copy()
        leg["chart"] = ident
        leg["chart_title"] = res.title or title
        leg["pdf"] = url
        leg["unit_detected"] = res.unit or "(none; thousand m3 assumed)"
        leg["warnings"] = " | ".join(res.warnings)
        legends.append(leg)
        review = config.ARCHIVE_DIR / f"{source_id}_{stem}_review.csv"
        review.parent.mkdir(parents=True, exist_ok=True)
        leg.to_csv(review, index=False)
        warns.extend(f"{title}: {w}" for w in res.warnings)
    df = pd.concat(data, ignore_index=True) if data else pd.DataFrame()
    lg = pd.concat(legends, ignore_index=True) if legends else pd.DataFrame()
    return df, lg, warns


def _history(source_id: str, ctx: RunContext, df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not df.empty:
        archive_frame(source_id, ctx.run_stamp, df[["date", "series", "kind", "chart", "chart_title", "unit",
                                                    "value_raw", "value"]])
    hist = load_archive(source_id)
    if hist.empty:
        return df, pd.DataFrame()
    hist["date"] = pd.to_datetime(hist["date"]).dt.normalize()
    return merge_latest(hist, ["chart", "series", "date"], "value")


def _pick_chart(df: pd.DataFrame, pattern: str) -> str | None:
    if df.empty:
        return None
    ids = df.groupby("chart").agg(title=("chart_title", "last"), days=("date", "nunique"))
    ids["match"] = ids["title"].map(lambda t: bool(re.search(pattern, norm(t))))
    ids = ids.sort_values(["match", "days"], ascending=False)
    return ids.index[0]


def _chart_checks(df: pd.DataFrame, source_id: str, total_pattern=r"\btotal\b") -> list[Check]:
    checks = []
    is_tot = df.series.map(lambda s: bool(re.search(total_pattern, norm(s))))
    tot, comp = df[is_tot], df[~is_tot]     # every parsed series, used or not: this checks the parse
    if tot.empty:
        checks.append(Check("Argentina", f"{source_id}: chart has no Total series", "info",
                            detail="components could not be checked against a chart total", source=source_id))
        return checks
    s = comp.groupby("date")["value"].sum()
    t = tot.groupby("date")["value"].sum()
    j = pd.concat([s.rename("sum"), t.rename("total")], axis=1).dropna()
    rel = (j["sum"] - j["total"]).abs() / j["total"].abs().clip(lower=1e-9)
    bad = j[rel > config.CHART_TOTAL_TOLERANCE_REL]
    checks.append(Check("Argentina", f"{source_id}: chart series sum vs chart total", "info" if bad.empty else "warn",
                        None, float(len(bad)), f"{len(j)} days compared; {len(bad)} off by more than "
                        f"{config.CHART_TOTAL_TOLERANCE_REL:.0%}", source_id))
    for d, r in bad.iterrows():
        checks.append(Check("Argentina", f"{source_id}: series sum != chart total", "warn", d,
                            float(r["sum"] - r["total"]), "mcm/d, sum of series minus chart total", source_id))
    return checks


def fetch_injection(ctx: RunContext) -> SourceData:
    sid = "enargas_injection"
    links = chart_links(config.ENARGAS_CHART_ITEMS, {"cat": 6})
    if not links:
        raise ValueError("no chart links found on cat=6 page")
    df, legend, warns = _parse_and_archive(sid, ctx, links)
    df, rev = _history(sid, ctx, df)
    chart = _pick_chart(df, config.ENARGAS_INJECTION_CHART)
    if chart is None:
        raise ValueError("no chart could be parsed: " + "; ".join(warns[:5]))
    df["used_chart"] = df["chart"] == chart
    df["excluded"] = df["series"].map(lambda s: bool(re.search(config.ENARGAS_INJECTION_EXCLUDE, norm(s))))
    sel = df[df.used_chart]
    data = sel[~sel.excluded].copy()
    checks = _chart_checks(sel, sid)
    for s in sorted(sel.loc[sel.excluded, "series"].unique()):
        if not re.search(r"\btotal\b", norm(s)):
            checks.append(Check("Argentina", "injection chart series looks like imports/LNG; excluded from production",
                                "warn", detail=s, source=sid))
    checks.append(Check("Argentina", "production assumption", "info", detail="Production = injection into the "
                        "transport system (chart series excluding import/LNG-named series) + producer-line exports; "
                        "if injection includes imports without naming them, production is overstated", source=sid))
    if not rev.empty:
        checks.append(Check("Argentina", "injection chart values revised between pulls", "info", None,
                            float(len(rev)), "latest pull used", sid))
    for w in warns:
        checks.append(Check("Argentina", "chart parser warning", "warn", detail=w, source=sid))
    returned = pd.DatetimeIndex(sorted(sel["date"].unique()))
    return SourceData(sid, {"data": data, "all_series": df, "legend": legend}, returned,
                      [f"chart used: {chart}"], checks)


def fetch_demand(ctx: RunContext) -> SourceData:
    sid = "enargas_demand"
    links = chart_links(config.ENARGAS_CHART_ITEMS, {"cat": 8})
    if not links:
        raise ValueError("no chart links found on cat=8 page")
    df, legend, warns = _parse_and_archive(sid, ctx, links)
    df, rev = _history(sid, ctx, df)
    chart = _pick_chart(df, config.ENARGAS_DEMAND_CHART)
    if chart is None:
        raise ValueError("no chart could be parsed: " + "; ".join(warns[:5]))
    df["used_chart"] = df["chart"] == chart
    df["sector"] = df["series"].map(lambda s: first_match(s, config.ENARGAS_DEMAND_SECTORS) or f"Other: {s}")
    df["excluded"] = df["series"].map(lambda s: bool(re.search(config.ENARGAS_DEMAND_EXCLUDE, norm(s))))
    sel = df[df.used_chart]
    data = sel[~sel.excluded].copy()
    checks = _chart_checks(sel, sid)
    for s in sorted(sel.loc[sel.excluded, "series"].unique()):
        if not re.search(r"\btotal\b", norm(s)):
            checks.append(Check("Argentina", "demand chart series excluded (exports come from partes)", "info",
                                detail=s, source=sid))
    for s in sorted(data.loc[data.sector.str.startswith("Other"), "series"].unique()):
        checks.append(Check("Argentina", "demand chart series not mapped to a sector; shown as its own line",
                            "warn", detail=s, source=sid))
    for w in warns:
        checks.append(Check("Argentina", "chart parser warning", "warn", detail=w, source=sid))
    returned = pd.DatetimeIndex(sorted(sel["date"].unique()))
    return SourceData(sid, {"data": data, "all_series": df, "legend": legend}, returned,
                      [f"chart used: {chart}"], checks)


def fetch_linepack(ctx: RunContext) -> SourceData:
    sid = "enargas_linepack"
    links = [(t, u) for t, u in chart_links(config.ENARGAS_CHARTS_PAGE)
             if re.search(config.ENARGAS_LINEPACK, norm(t) + " " + norm(u))]
    if not links:
        raise ValueError("no linepack chart link found on dod-graficos-de-programacion.php")
    df, legend, warns = _parse_and_archive(sid, ctx, links)
    if df.empty and warns:
        raise SourceUnavailable("; ".join(warns[:3]), "failed")
    df, rev = _history(sid, ctx, df)
    if df.empty:
        raise ValueError("linepack chart parsed to no data")
    cand = df[df.series.map(lambda s: bool(re.search(config.ENARGAS_LINEPACK, norm(s)))
                            and not re.search(config.ENARGAS_LINEPACK_EXCLUDE, norm(s)))]
    if cand.empty:
        names = df.series.unique()
        cand = df if len(names) == 1 else cand
    if cand.empty:
        raise ValueError(f"linepack series not identified among {list(df.series.unique())}")
    level = cand.groupby("date")["value"].mean().sort_index()
    full = level.asfreq("D")
    change = (full - full.shift(1)).dropna()          # blank if either day is missing
    data = pd.DataFrame({"date": change.index, "value": change.values, "series": "linepack change"})
    levels = pd.DataFrame({"date": level.index, "value": level.values, "series": "linepack level"})
    checks = [Check("Argentina", "chart parser warning", "warn", detail=w, source=sid) for w in warns]
    return SourceData(sid, {"data": data, "level": levels, "legend": legend}, pd.DatetimeIndex(change.index),
                      ["linepack change derived day on day from chart level"], checks)
