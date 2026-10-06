"""Public ("publish to web") Power BI reports: list visuals, query them, decode results.

Flow: page HTML -> ``app.powerbi.com/view?r=<base64 json {k: resource key, t: tenant}>``
-> cluster from ``api.powerbi.com/public/routing/cluster/<tenant>`` ->
``modelsAndExploration`` (pages, visuals and their prototype queries) -> ``querydata``
with a flat table binding over the visual's own query -> DSR result decoded to rows,
following restart tokens until all rows are read.

CLI, to inspect a page and confirm the visual auto-pick before pinning it in
``config.BMC_PINS``::

    python -m latam_gas_snd.pbi <page url or powerbi view url> [--visual ID] [--rows 10]
"""
from __future__ import annotations

import argparse
import base64
import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlparse

import pandas as pd

from . import config
from .util import get, norm, post

WINDOW = 30000
MAX_PAGES = 100


@dataclass
class Visual:
    page: str
    id: str
    type: str
    title: str
    query: dict
    names: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return f"[{self.page}] {self.type} {self.id} '{self.title}': {', '.join(self.names)}"


@dataclass
class Report:
    view_url: str
    key: str
    api: str
    model_id: int
    dataset_id: str
    report_id: str
    visuals: list[Visual]


def report_urls(html: str) -> list[str]:
    urls = re.findall(r"https://app\.powerbi\.com/view\?r=[A-Za-z0-9%=_\-]+(?:&amp;[^\"'\s<>]*|&[^\"'\s<>]*)?", html)
    return list(dict.fromkeys(u.replace("&amp;", "&") for u in urls))


def decode_key(view_url: str) -> dict:
    r = parse_qs(urlparse(view_url).query)["r"][0]
    r = unquote(r)
    pad = "=" * (-len(r) % 4)
    return json.loads(base64.b64decode(r + pad).decode("utf-8"))


def api_base(tenant: str) -> str:
    j = get(config.POWERBI_ROUTING.format(tenant=tenant)).json()
    uri = j["FixedClusterUri"].rstrip("/")
    host = urlparse(uri).netloc.lower().replace("-redirect", "-api")
    return f"https://{host}"


def _title(sv: dict) -> str:
    try:
        v = sv["vcObjects"]["title"][0]["properties"]["text"]["expr"]["Literal"]["Value"]
        return v.strip("'")
    except (KeyError, IndexError, TypeError):
        return ""


def _select_name(s: dict) -> str:
    return s.get("NativeReferenceName") or s.get("Name") or ""


def load_report(view_url: str) -> Report:
    k = decode_key(view_url)
    key, tenant = k["k"], k["t"]
    api = api_base(tenant)
    mae = get(f"{api}/public/reports/{key}/modelsAndExploration", params={"preferReadOnlySession": "true"},
              headers={"X-PowerBI-ResourceKey": key}).json()
    model = mae["models"][0]
    exploration = mae["exploration"]
    visuals = []
    for sec in exploration.get("sections", []):
        for vc in sec.get("visualContainers", []):
            try:
                cfg = json.loads(vc["config"])
            except (KeyError, ValueError):
                continue
            sv = cfg.get("singleVisual") or {}
            q = sv.get("prototypeQuery")
            if not q:
                continue
            visuals.append(Visual(sec.get("displayName", ""), cfg.get("name", ""), sv.get("visualType", ""),
                                  _title(sv), q, [_select_name(s) for s in q.get("Select", [])]))
    report_id = exploration.get("report", {}).get("objectId") or mae.get("reportId", "")
    return Report(view_url, key, api, model["id"], model.get("dbName", ""), report_id, visuals)


def score(v: Visual, keywords: list[str] = ()) -> int:
    if re.search(r"slicer|card|textbox|image|shape|button", v.type, re.I):
        return -100
    names = [norm(n) for n in v.names]
    has_date = any(re.search(r"fecha|date|\bdia\b|day|periodo", n) for n in names)
    has_measure = any(("Measure" in s or "Aggregation" in s) for s in v.query.get("Select", []))
    if not (has_date and has_measure):
        return -1
    s = 10 + 3 * len(names)
    if re.search(r"table|pivot|matrix", v.type, re.I):
        s += 10
    blob = norm(" ".join([v.page, v.title, *v.names]))
    s += sum(5 for kw in keywords if norm(kw) in blob)
    return s


def pick(report: Report, keywords: list[str] = (), pin: dict | None = None) -> Visual:
    if pin:
        for v in report.visuals:
            if v.id == pin.get("visual") and (not pin.get("page") or norm(v.page) == norm(pin["page"])):
                return v
        raise ValueError(f"pinned visual {pin} not found in report")
    ranked = sorted(report.visuals, key=lambda v: score(v, keywords), reverse=True)
    if not ranked or score(ranked[0], keywords) < 0:
        raise ValueError("no visual with a date column and a measure")
    return ranked[0]


def build_body(report: Report, v: Visual, restart: list | None = None) -> dict:
    n = len(v.query.get("Select", []))
    window: dict = {"Count": WINDOW}
    if restart:
        window["RestartTokens"] = restart
    cmd = {"SemanticQueryDataShapeCommand": {
        "Query": v.query,
        "Binding": {"Primary": {"Groupings": [{"Projections": list(range(n))}]},
                    "DataReduction": {"DataVolume": 3, "Primary": {"Window": window}},
                    "Version": 1},
        "ExecutionMetricsKind": 1}}
    return {"version": "1.0.0",
            "queries": [{"Query": {"Commands": [cmd]}, "QueryId": "",
                         "ApplicationContext": {"DatasetId": report.dataset_id,
                                                "Sources": [{"ReportId": report.report_id, "VisualId": v.id}]}}],
            "cancelQueries": [], "modelId": report.model_id}


def decode_dsr(resp: dict) -> tuple[list[str], list[list], list | None]:
    """Decode a querydata response: (column names, rows, restart tokens)."""
    result = resp["results"][0]["result"]
    data = result["data"]
    if "dsr" not in data:
        raise ValueError(f"query error: {json.dumps(data)[:300]}")
    sel = {s["Value"]: s.get("Name", s["Value"]) for s in data.get("descriptor", {}).get("Select", [])}
    ds = data["dsr"]["DS"][0]
    if "odata.error" in ds or "Error" in ds:
        raise ValueError(f"query error: {json.dumps(ds)[:300]}")
    vd = ds.get("ValueDicts", {})
    dm = next((p["DM0"] for p in ds.get("PH", []) if "DM0" in p), [])
    schema: list[dict] | None = None
    prev: list = []
    rows = []
    for row in dm:
        if "S" in row:
            schema = row["S"]
            prev = [None] * len(schema)
        if schema is None:
            continue
        c = row.get("C", [])
        rep = row.get("R", 0)
        nul = row.get("Ø", 0)
        vals, ci = [], 0
        for i, col in enumerate(schema):
            if rep >> i & 1:
                v = prev[i]
            elif nul >> i & 1:
                v = None
            else:
                v = c[ci] if ci < len(c) else None
                ci += 1
                if "DN" in col and isinstance(v, int):
                    v = vd[col["DN"]][v]
            vals.append(v)
        prev = vals
        rows.append(vals)
    names = [sel.get(col["N"], col["N"]) for col in (schema or [])]
    types = [col.get("T") for col in (schema or [])]
    # epoch-ms datetimes
    for i, t in enumerate(types):
        if t == 7 or re.search(r"fecha|date|dia|day", norm(names[i])):
            for r in rows:
                if isinstance(r[i], (int, float)) and abs(r[i]) > 1e11:
                    r[i] = pd.Timestamp(r[i], unit="ms")
    return names, rows, ds.get("RT")


def run_visual(report: Report, v: Visual) -> pd.DataFrame:
    url = f"{report.api}/public/reports/querydata"
    rows_all, names, restart = [], None, None
    for _ in range(MAX_PAGES):
        resp = post(url, params={"synchronous": "true"}, json=build_body(report, v, restart),
                    headers={"X-PowerBI-ResourceKey": report.key}).json()
        names, rows, rt = decode_dsr(resp)
        rows_all += rows
        if not rt or not rows:
            break
        restart = rt
    return pd.DataFrame(rows_all, columns=names)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Inspect a public Power BI report embedded in a page")
    ap.add_argument("url")
    ap.add_argument("--visual", help="query this visual id instead of the auto-pick")
    ap.add_argument("--keywords", default="", help="comma-separated words that should appear in the visual")
    ap.add_argument("--rows", type=int, default=10)
    a = ap.parse_args(argv)
    views = [a.url] if "app.powerbi.com/view" in a.url else report_urls(get(a.url).text)
    if not views:
        raise SystemExit("no app.powerbi.com/view?r= embed found on the page")
    kws = [k for k in a.keywords.split(",") if k]
    for view in views:
        rep = load_report(view)
        print(f"report {rep.report_id} dataset {rep.dataset_id} model {rep.model_id} api {rep.api}")
        for v in sorted(rep.visuals, key=lambda v: score(v, kws), reverse=True):
            print(f"  score {score(v, kws):4d}  {v.describe()}")
        v = next((x for x in rep.visuals if x.id == a.visual), None) if a.visual else pick(rep, kws)
        if v is None:
            raise SystemExit(f"visual {a.visual} not found")
        print(f"\nquerying: {v.describe()}")
        df = run_visual(rep, v)
        print(df.head(a.rows).to_string())
        print(f"... {len(df)} rows")
        print(f'\npin with: BMC_PINS["<report id>"] = {{"page": {v.page!r}, "visual": {v.id!r}}}')


if __name__ == "__main__":
    main()
