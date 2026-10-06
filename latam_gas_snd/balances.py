"""Country balances and cross-border flows built from fetched sources.

Rules applied throughout:
* a line is blank on days its source(s) did not return; a source that is off or failed
  leaves every line depending on it blank;
* lines combining sources (e.g. Bolivian-origin border flow) are blank unless every
  source they depend on returned that day;
* nothing is back-solved: residuals are left to the workbook formula;
* inconsistencies are added as checks, values are never adjusted.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from . import config
from .model import (DEMAND, MEMO, SUPPLY, Check, CountryBalance, Line, RunContext, SourceData, blank,
                    line_from)
from .util import first_match, norm

COUNTRIES = ["Argentina", "Brazil", "Colombia", "Bolivia", "Chile", "Uruguay"]


def _add(*series: pd.Series) -> pd.Series:
    """Sum that is blank if any part is blank (a line needs all its sources)."""
    out = series[0].copy()
    for s in series[1:]:
        out = out + s
    return out


def _cp(cp: str):
    return lambda d: d["counterparty"] == cp


def _present(sd: SourceData | None, col: str, table: str = "data") -> list[str]:
    if sd is None or table not in sd.tables or col not in sd.tables[table]:
        return []
    return sorted(sd.tables[table][col].dropna().unique())


def _with(sd: SourceData | None, df: pd.DataFrame) -> SourceData | None:
    """Copy of a SourceData with a replaced main table (sources are never mutated)."""
    if sd is None:
        return None
    return SourceData(sd.source_id, {**sd.tables, "data": df}, sd.returned_dates)


def _pivot(df: pd.DataFrame | None, col: str, index: pd.DatetimeIndex, value: str = "value") -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(index=index)
    p = df.pivot_table(index="date", columns=col, values=value, aggfunc="sum")
    return p.reindex(index)


# =========================================================================== Argentina
def argentina(ctx: RunContext) -> CountryBalance:
    idx = ctx.index
    imp, din, fue = ctx.data("enargas_imports"), ctx.data("enargas_exp_dentro"), ctx.data("enargas_exp_fuera")
    inj, dem, lp = ctx.data("enargas_injection"), ctx.data("enargas_demand"), ctx.data("enargas_linepack")
    L: list[Line] = []
    L.append(Line("ar_prod_inj", "Production: injection into transport system", SUPPLY, ("enargas_injection",),
                  line_from(inj, idx), "Sum of pipeline series in the injection chart, import/LNG series excluded"))
    L.append(Line("ar_prod_fuera", "Production: producer-line exports (outside system)", SUPPLY,
                  ("enargas_exp_fuera",), line_from(fue, idx), "Production = injection + producer-line exports"))
    for cp, label in [("Bolivia", "Pipeline imports: Bolivia"), ("Chile", "Pipeline imports: Chile")]:
        L.append(Line(f"ar_imp_{cp.lower()}", label, SUPPLY, ("enargas_imports",), line_from(imp, idx, _cp(cp))))
    for cp, label in [("LNG Escobar", "LNG sendout: Escobar"), ("LNG Bahia Blanca", "LNG sendout: Bahía Blanca")]:
        L.append(Line(f"ar_{cp.lower().replace(' ', '_')}", label, SUPPLY, ("enargas_imports",),
                      line_from(imp, idx, _cp(cp))))
    for cp in ("LNG other", "Unclassified"):
        if cp in _present(imp, "counterparty"):
            L.append(Line(f"ar_imp_{cp.lower().replace(' ', '_')}", f"Imports: {cp} (route not mapped)", SUPPLY,
                          ("enargas_imports",), line_from(imp, idx, _cp(cp)), "see Checks for the route names"))
    # demand by sector
    sectors = [s for _, s in config.ENARGAS_DEMAND_SECTORS]
    sectors += [s for s in _present(dem, "sector") if s not in sectors]
    for s in sectors:
        L.append(Line(f"ar_dem_{s.lower()[:20]}", f"Demand: {s}", DEMAND, ("enargas_demand",),
                      line_from(dem, idx, lambda d, s=s: d["sector"] == s)))
    for sd, sid, where in ((din, "enargas_exp_dentro", "in system"), (fue, "enargas_exp_fuera", "producer lines")):
        cps = ["Chile", "Uruguay", "Brazil", "Brazil via Bolivia"] if sid == "enargas_exp_dentro" else ["Chile"]
        cps += [c for c in _present(sd, "counterparty") if c not in cps]
        for cp in cps:
            L.append(Line(f"ar_exp_{sid[-6:]}_{cp.lower().replace(' ', '_')}", f"Exports to {cp} ({where})", DEMAND,
                          (sid,), line_from(sd, idx, _cp(cp))))
    L.append(Line("ar_linepack", "Linepack change", DEMAND, ("enargas_linepack",), line_from(lp, idx),
                  "Day-on-day change in chart linepack level; positive = packing"))
    # memo
    for sd, sid, lab in ((imp, "enargas_imports", "imports"), (din, "enargas_exp_dentro", "exports in system"),
                         (fue, "enargas_exp_fuera", "exports producer lines")):
        L.append(Line(f"ar_memo_total_{sid}", f"Memo: ENARGAS Total column, {lab}", MEMO, (sid,),
                      _total_line(sd, idx), "Partes Total column as published; routes must sum to it"))
    for sd, sid, lab in ((inj, "enargas_injection", "injection"), (dem, "enargas_demand", "demand")):
        if sd is not None and _chart_total_rows(sd).shape[0]:
            s = _chart_total_rows(sd).groupby("date")["value"].sum().reindex(idx)
            L.append(Line(f"ar_memo_chart_total_{lab}", f"Memo: {lab} chart total series", MEMO, (sid,), s))
    if lp is not None:
        lvl = lp.tables["level"].set_index("date")["value"].reindex(idx)
        L.append(Line("ar_memo_linepack_level", "Memo: linepack level (mcm)", MEMO, ("enargas_linepack",), lvl,
                      frequency="daily level"))
    bal = CountryBalance("Argentina", L, idx)
    # drill-down detail
    for sd, name in ((imp, "Imports by route"), (din, "Exports in system by route"),
                     (fue, "Exports producer lines by route")):
        if sd is not None:
            p = _pivot(sd.data, "route", idx)
            p["ENARGAS Total"] = _total_line(sd, idx)
            bal.detail[name] = p
    for sd, name, col in ((inj, "Injection by pipeline", "series"), (dem, "Demand by sector (chart)", "series")):
        if sd is not None:
            a = sd.tables["all_series"]
            bal.detail[name] = _pivot(a[a.used_chart], col, idx)
    if lp is not None:
        bal.detail["Linepack"] = pd.DataFrame({"level (mcm)": lvl, "change (mcm/d)": line_from(lp, idx)})
    legends = [sd.tables["legend"].assign(source=sd.source_id) for sd in (inj, dem, lp) if sd is not None
               and "legend" in sd.tables and not sd.tables["legend"].empty]
    if legends:
        bal.detail["Chart legend review"] = pd.concat(legends, ignore_index=True)
    routes = []
    for sd in (imp, din, fue):
        if sd is not None:
            g = sd.data.groupby(["route", "counterparty", "methanex"])["date"].agg(["min", "max", "nunique"])
            routes.append(g.reset_index().assign(source=sd.source_id))
    if routes:
        bal.detail["Route map"] = pd.concat(routes, ignore_index=True).rename(
            columns={"min": "first date", "max": "last date", "nunique": "days"})
    _bolivia_flag(bal, imp)
    return bal


def _total_line(sd: SourceData | None, idx) -> pd.Series:
    if sd is None or sd.tables.get("totals") is None or sd.tables["totals"].empty:
        return blank(idx)
    return sd.tables["totals"].groupby("date")["value"].sum().reindex(idx)


def _chart_total_rows(sd: SourceData) -> pd.DataFrame:
    a = sd.tables.get("all_series", pd.DataFrame())
    if a.empty:
        return a
    return a[a.used_chart & a.series.map(lambda s: bool(re.search(r"\btotal\b", norm(s))))]


def _bolivia_flag(bal: CountryBalance, imp: SourceData | None):
    if imp is None:
        return
    b = imp.data[imp.data.counterparty == "Bolivia"]
    b = b[b.value > 0]
    if b.empty:
        return
    by_day = b.groupby("date")["value"].sum()
    recent = by_day[by_day.index >= pd.Timestamp("2024-01-01")]
    bal.checks.append(Check(
        "Argentina", "Bolivian imports into Argentina: origin unverified", "warn", by_day.index.max(),
        float(by_day.iloc[-1]),
        f"{len(by_day)} days > 0 ({by_day.index.min().date()} to {by_day.index.max().date()}); since 2024: "
        f"{len(recent)} days, {recent.min() if len(recent) else 0:.2f}-{recent.max() if len(recent) else 0:.2f} mcm/d. "
        "Treated as physical Bolivian supply; check against YPFB whether Bolivian-origin gas or a swap tied to "
        "Argentine exports via Bolivia.", "enargas_imports"))


# =========================================================================== Brazil
BR_SUPPLY = [("production", "Production into grid"), ("lng", "LNG sendout (regas into grid)"),
             ("uruguaiana", "Pipeline imports: Argentina at Uruguaiana"), ("biomethane", "Biomethane injection")]
BR_DEMAND = [("power", "Power (thermal plants on grid)"), ("refinery", "Refineries"), ("fertiliser", "Fertiliser"),
             ("city_gate", "City gates (industry, residential, commercial, CNG)"), ("export", "Pipeline exports")]


def _via_bolivia(ctx: RunContext, idx) -> tuple[pd.Series, tuple[str, ...]]:
    din, fue = ctx.data("enargas_exp_dentro"), ctx.data("enargas_exp_fuera")
    s = line_from(din, idx, _cp("Brazil via Bolivia"))
    sources: tuple[str, ...] = ("enargas_exp_dentro",)
    if "Brazil via Bolivia" in _present(fue, "counterparty"):
        s = _add(s, line_from(fue, idx, _cp("Brazil via Bolivia")))
        sources += ("enargas_exp_fuera",)
    return s, sources


def brazil(ctx: RunContext) -> CountryBalance:
    idx = ctx.index
    anp, ons = ctx.data("anp"), ctx.data("ons")
    vr = (lambda d: d["measure"] == "Volume Realizado")
    L: list[Line] = []
    border = line_from(anp, idx, lambda d: vr(d) & (d["category"] == "bolivia_border"))
    via_bo, via_src = _via_bolivia(ctx, idx)
    bo_origin = border - via_bo
    for cat, label in BR_SUPPLY[:2]:
        L.append(Line(f"br_{cat}", label, SUPPLY, ("anp",),
                      line_from(anp, idx, lambda d, c=cat: vr(d) & (d["category"] == c))))
    L.append(Line("br_imp_bolivia", "Pipeline imports: Bolivia (Bolivian-origin, derived)", SUPPLY,
                  ("anp", *via_src), bo_origin,
                  "ANP Corumbá + Cáceres receipts less ENARGAS exports 'por Bolivia'; negative values kept and flagged"))
    L.append(Line("br_imp_ar_via_bo", "Pipeline imports: Argentina via Bolivia (Argentine-origin)", SUPPLY,
                  via_src, via_bo, "ENARGAS exports routed through Bolivia; part of the ANP border receipt"))
    for cat, label in BR_SUPPLY[2:]:
        L.append(Line(f"br_{cat}", label, SUPPLY, ("anp",),
                      line_from(anp, idx, lambda d, c=cat: vr(d) & (d["category"] == c))))
    if anp is not None and ((anp.data.category == "unclassified") & (anp.data.direction != "delivery")).any():
        L.append(Line("br_unc_receipt", "Other receipts (point not classified)", SUPPLY, ("anp",),
                      line_from(anp, idx, lambda d: vr(d) & (d["category"] == "unclassified")
                                & (d["direction"] != "delivery")), "see BR checks / Point list"))
    for cat, label in BR_DEMAND:
        L.append(Line(f"br_{cat}", label, DEMAND, ("anp",),
                      line_from(anp, idx, lambda d, c=cat: vr(d) & (d["category"] == c))))
    if anp is not None and ((anp.data.category == "unclassified") & (anp.data.direction == "delivery")).any():
        L.append(Line("br_unc_delivery", "Other deliveries (point not classified)", DEMAND, ("anp",),
                      line_from(anp, idx, lambda d: vr(d) & (d["category"] == "unclassified")
                                & (d["direction"] == "delivery")), "see BR checks / Point list"))
    L.append(Line("br_linepack", "Linepack change (Empacotamento)", DEMAND, ("anp",),
                  line_from(anp, idx, lambda d: d["measure"] == "Empacotamento"),
                  "ANP Empacotamento as published; sign convention to verify (positive taken as packing)"))
    # memo
    L.append(Line("br_memo_border", "Memo: ANP Bolivia border receipts (Corumbá + Cáceres)", MEMO, ("anp",), border))
    din = ctx.data("enargas_exp_dentro")
    L.append(Line("br_memo_enargas_uru", "Memo: ENARGAS exports to Brazil (mirror of Uruguaiana)", MEMO,
                  ("enargas_exp_dentro",), line_from(din, idx, _cp("Brazil"))))
    L.append(Line("br_memo_ons", "Memo: ONS gas-fired generation, gas equivalent", MEMO, ("ons",),
                  line_from(ons, idx), "MWh x 3.6 / 45% / 0.0389 GJ/m3; grid plants dispatched by ONS"))
    if anp is not None:
        inter = anp.data[(anp.data.measure == "Volume Realizado") & (anp.data.category == "interconnection")]
        if not inter.empty:
            sign = inter["direction"].map({"receipt": 1.0, "delivery": -1.0, "unknown": 0.0})
            net = (inter["value"] * sign).groupby(inter["date"]).sum().reindex(anp.returned_dates).fillna(0)
            L.append(Line("br_memo_transfers", "Memo: internal transfers net (dropped, should be ~0)", MEMO,
                          ("anp",), net.reindex(idx)))
    bal = CountryBalance("Brazil", L, idx)
    # checks
    neg = bo_origin[bo_origin < -config.BR_BOLIVIA_NEGATIVE_TOLERANCE_MCM]
    bal.checks.append(Check("Brazil", "Bolivian-origin border flow negative (derived)", "info" if neg.empty else "warn",
                            None, float(len(neg)), f"{bo_origin.notna().sum()} days derived; {len(neg)} below "
                            f"-{config.BR_BOLIVIA_NEGATIVE_TOLERANCE_MCM} mcm/d (kept, not clipped)", "anp"))
    for d, v in neg.items():
        bal.checks.append(Check("Brazil", "Bolivian-origin border flow negative", "warn", d, float(v),
                                "ANP border receipts < ENARGAS exports via Bolivia: timing or measurement mismatch",
                                "anp"))
    uru = line_from(anp, idx, lambda d: vr(d) & (d["category"] == "uruguaiana"))
    mirror = line_from(din, idx, _cp("Brazil"))
    diff = (uru - mirror).dropna()
    if not diff.empty:
        bal.checks.append(Check("Brazil", "Uruguaiana: ANP receipt minus ENARGAS export", "info", None,
                                float(diff.abs().mean()), f"mean |diff| over {len(diff)} days, mcm/d; "
                                f"max {diff.abs().max():.3f}", "anp"))
    if anp is not None:
        _brazil_detail(bal, anp, idx)
    return bal


def _brazil_detail(bal: CountryBalance, anp: SourceData, idx):
    d = anp.data
    vr = d[d.measure == "Volume Realizado"]
    signed = vr.assign(value=np.where(vr.direction == "delivery", -vr.value, vr.value))
    bal.detail["By category"] = _pivot(vr.assign(k=vr.category + " (" + vr.direction + ")"), "k", idx)
    bal.detail["By category & state"] = _pivot(vr.assign(k=vr.category + " | " + vr.state.astype(str)), "k", idx)
    lab = vr.transporter.astype(str) + " | " + vr.point.astype(str) + " | " + vr.direction
    bal.detail["By point"] = _pivot(vr.assign(k=lab), "k", idx)
    pl = d.groupby(["transporter", "point", "direction", "category", "rule", "state", "measure"]).agg(
        first_date=("date", "min"), last_date=("date", "max"), days=("date", "nunique"),
        mean_mcm_d=("value", "mean")).reset_index()
    bal.detail["Point list"] = pl
    inter = signed[signed.category == "interconnection"].groupby("date")["value"].sum()
    monthly = vr[vr.category != "interconnection"].assign(month=vr.date.dt.to_period("M").dt.to_timestamp())
    m = monthly.groupby(["month", "category", "direction"])["value"].mean().unstack(["category", "direction"])
    days = monthly.groupby("month")["date"].nunique()
    m.columns = [f"{c} ({dr}) avg mcm/d" for c, dr in m.columns]
    m.insert(0, "days in ANP", days)
    m["MME Boletim grid balance"] = np.nan   # not fetched: see README open issues
    bal.detail["BR checks"] = pd.DataFrame({
        "internal transfers net (mcm/d)": inter.reindex(idx),
        "flagged": (inter.abs() > config.BR_TRANSFER_TOLERANCE_MCM).reindex(idx)})
    bal.detail["BR monthly"] = m


# =========================================================================== Colombia
def _dim_cols(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c not in ("date", "value", "value_raw", "unit", "measure", "run")]


def _best_dim(df: pd.DataFrame, rules) -> str | None:
    best, hits = None, 0
    for c in _dim_cols(df):
        vals = df[c].dropna().astype(str).unique()
        h = sum(1 for v in vals if first_match(v, rules))
        if h > hits:
            best, hits = c, h
    return best


def _injection_type(df: pd.DataFrame) -> pd.Series:
    typ = next((c for c in _dim_cols(df) if df[c].astype(str).map(norm).str.contains(r"import|produc").mean() > 0.5),
               None)
    if typ is not None:
        return df[typ].astype(str).map(lambda v: "import" if "import" in norm(v) else "production")
    joined = df[_dim_cols(df)].astype(str).agg(" | ".join, axis=1)
    return joined.map(lambda v: "import" if re.search(config.CO_IMPORT_ENTRY, norm(v)) else "production")


def colombia(ctx: RunContext) -> CountryBalance:
    idx = ctx.index
    inj, nin = ctx.data("bmc_injection"), ctx.data("bmc_imported_not_injected")
    off, mkt, tra, xm = (ctx.data("bmc_offtake_snt"), ctx.data("bmc_offtake_marketers"),
                         ctx.data("bmc_offtake_tramo"), ctx.data("xm"))
    L: list[Line] = []
    checks: list[Check] = []
    if inj is not None:
        inj = _with(inj, inj.data.assign(kind=_injection_type(inj.data)))
    L.append(Line("co_prod", "Production injected into SNT", SUPPLY, ("bmc_injection",),
                  line_from(inj, idx, lambda d: d["kind"] == "production")))
    L.append(Line("co_lng", "LNG / imports injected into SNT", SUPPLY, ("bmc_injection",),
                  line_from(inj, idx, lambda d: d["kind"] == "import"), "SPEC Cartagena regas and any pipeline imports"))
    L.append(Line("co_imp_not_inj", "Imported gas not injected into SNT", SUPPLY, ("bmc_imported_not_injected",),
                  line_from(nin, idx), "Declared by importers; used outside the SNT"))
    has_power = False
    if off is not None:
        sc = _best_dim(off.data, config.CO_SECTORS)
        if sc is None:
            off = _with(off, off.data.assign(sector="Total offtake (no sector published)"))
            checks.append(Check("Colombia", "BMC offtake has no sector dimension", "warn",
                                detail=f"dimensions {_dim_cols(off.data)}", source="bmc_offtake_snt"))
        else:
            off = _with(off, off.data.assign(
                sector=off.data[sc].astype(str).map(lambda v: first_match(v, config.CO_SECTORS) or f"Other: {v}")))
        sectors = [s for _, s in config.CO_SECTORS if s in set(off.data.sector)]
        sectors += sorted(s for s in set(off.data.sector) if s not in sectors)
        has_power = "Power" in sectors
        for s in sectors:
            L.append(Line(f"co_dem_{s.lower()[:24]}", f"Demand: {s}", DEMAND, ("bmc_offtake_snt",),
                          line_from(off, idx, lambda d, s=s: d["sector"] == s)))
    else:
        L.append(Line("co_dem_offtake", "Demand: offtake from SNT (by sector where published)", DEMAND,
                      ("bmc_offtake_snt",), blank(idx)))
    L.append(Line("co_dem_imp_outside", "Demand: imported gas used outside SNT", DEMAND, ("bmc_imported_not_injected",),
                  line_from(nin, idx), "Same report as the supply line: consumed outside the SNT"))
    xm_gas = line_from(xm, idx, lambda d: d["is_gas"]) if xm is not None and xm.data["is_gas"].any() else blank(idx)
    xm_side = DEMAND if (off is not None and not has_power and xm is not None and xm.data["is_gas"].any()) else MEMO
    L.append(Line("co_xm", ("Demand: Power (XM declared thermal gas burn)" if xm_side == DEMAND
                            else "Memo: XM declared thermal gas burn"), xm_side, ("xm",), xm_gas,
                  "XM in demand only when BMC offtake is available and has no thermal split"))
    for sd, sid, lab in ((inj, "bmc_injection", "BMC injection total"), (mkt, "bmc_offtake_marketers",
                         "BMC offtake by marketers, total"), (tra, "bmc_offtake_tramo", "BMC offtake by segment, total")):
        L.append(Line(f"co_memo_{sid}", f"Memo: {lab}", MEMO, (sid,), line_from(sd, idx)))
    if xm is not None and not xm.data["is_gas"].any():
        L.append(Line("co_memo_xm_all", "Memo: XM thermal fuel burn, all fuels (fuel not identified)", MEMO, ("xm",),
                      line_from(xm, idx)))
    bal = CountryBalance("Colombia", L, idx, checks=checks)
    if inj is not None:
        dims = _dim_cols(inj.data)
        key = inj.data[dims].astype(str).agg(" | ".join, axis=1) if dims else inj.data["kind"]
        bal.detail["Injection by entry point"] = _pivot(inj.data.assign(k=key + " [" + inj.data["kind"] + "]"), "k", idx)
    for sd, name in ((off, "Offtake by sector"), (mkt, "Offtake by marketer"), (tra, "Offtake by segment"),
                     (nin, "Imported not injected")):
        if sd is not None:
            dims = [c for c in _dim_cols(sd.data) if c != "sector"]
            col = "sector" if name == "Offtake by sector" else None
            key = sd.data[col] if col else (sd.data[dims].astype(str).agg(" | ".join, axis=1) if dims else "total")
            bal.detail[name] = _pivot(sd.data.assign(k=key), "k", idx)
    if xm is not None:
        g = xm.data[xm.data.is_gas] if xm.data.is_gas.any() else xm.data
        bal.detail["XM burn by plant"] = _pivot(g.assign(k=g.plant + " | " + g.fuel), "k", idx)
    if off is not None and xm is not None and has_power and xm.data["is_gas"].any():
        bp = line_from(off, idx, lambda d: d["sector"] == "Power")
        diff = (bp - xm_gas).dropna()
        if not diff.empty:
            bal.checks.append(Check("Colombia", "BMC power offtake minus XM gas burn", "info", None,
                                    float(diff.mean()), f"mean over {len(diff)} days, mcm/d", "xm"))
    return bal


# =========================================================================== Bolivia / Chile / Uruguay
def bolivia(ctx: RunContext) -> CountryBalance:
    idx = ctx.index
    imp, anp = ctx.data("enargas_imports"), ctx.data("anp")
    via_bo, via_src = _via_bolivia(ctx, idx)
    border = line_from(anp, idx, lambda d: (d["measure"] == "Volume Realizado") & (d["category"] == "bolivia_border"))
    L = [
        Line("bo_prod", "Production", SUPPLY, (), blank(idx), "No daily public source found: gap left open",
             frequency="none"),
        Line("bo_dem", "Domestic demand", DEMAND, (), blank(idx), "No daily public source found: gap left open",
             frequency="none"),
        Line("bo_exp_ar", "Exports to Argentina", DEMAND, ("enargas_imports",),
             line_from(imp, idx, _cp("Bolivia")), "ENARGAS imports from Bolivia"),
        Line("bo_exp_br", "Exports to Brazil (Bolivian-origin, derived)", DEMAND, ("anp", *via_src), border - via_bo,
             "ANP border receipts less Argentine gas in transit"),
        Line("bo_memo_transit", "Memo: Argentine gas in transit to Brazil", MEMO, via_src, via_bo),
        Line("bo_memo_border", "Memo: ANP Bolivia border receipts", MEMO, ("anp",), border),
    ]
    return CountryBalance("Bolivia", L, idx)


def chile(ctx: RunContext) -> CountryBalance:
    idx = ctx.index
    din, fue, imp, kp = (ctx.data("enargas_exp_dentro"), ctx.data("enargas_exp_fuera"), ctx.data("enargas_imports"),
                         ctx.data("kpler"))
    meth = lambda d: d["methanex"]  # noqa: E731
    L = [
        Line("cl_imp_ar_in", "Pipeline imports: Argentina (in-system routes)", SUPPLY, ("enargas_exp_dentro",),
             line_from(din, idx, _cp("Chile"))),
        Line("cl_imp_ar_pl", "Pipeline imports: Argentina (producer lines)", SUPPLY, ("enargas_exp_fuera",),
             line_from(fue, idx, _cp("Chile"))),
        Line("cl_lng", "LNG imports (Kpler cargo discharges)", SUPPLY, ("kpler",), line_from(kp, idx),
             "Cargo arrivals, not regas sendout; blank without Kpler access"),
        Line("cl_methanex", "Demand: Methanex feedstock", DEMAND, ("enargas_exp_dentro", "enargas_exp_fuera"),
             _add(line_from(din, idx, meth), line_from(fue, idx, meth)), "ENARGAS Methanex export routes"),
        Line("cl_exp_ar", "Pipeline exports to Argentina", DEMAND, ("enargas_imports",),
             line_from(imp, idx, _cp("Chile")), "ENARGAS imports from Chile"),
    ]
    bal = CountryBalance("Chile", L, idx,
                         residual_label="Residual = power, distribution, industry + fuel, losses, stat diff, missing")
    r = bal.residual()
    have = pd.concat([ln.series for ln in L if ln.side != MEMO], axis=1).notna().any(axis=1)
    neg = r[have & (r < 0)]
    if len(neg):
        bal.checks.append(Check("Chile", "Residual (power/distribution/industry) negative", "warn", None,
                                float(len(neg)), "days where Methanex + exports exceed recorded supply; LNG blank or "
                                "timing mismatch", "kpler"))
    return bal


def uruguay(ctx: RunContext) -> CountryBalance:
    idx = ctx.index
    din, fue = ctx.data("enargas_exp_dentro"), ctx.data("enargas_exp_fuera")
    L = [Line("uy_imp_ar", "Pipeline imports: Argentina (Cruz del Sur, PetroUruguay)", SUPPLY, ("enargas_exp_dentro",),
              line_from(din, idx, _cp("Uruguay")))]
    if "Uruguay" in _present(fue, "counterparty"):
        L.append(Line("uy_imp_ar_pl", "Pipeline imports: Argentina (producer lines)", SUPPLY, ("enargas_exp_fuera",),
                      line_from(fue, idx, _cp("Uruguay"))))
    sup = pd.concat([ln.series for ln in L], axis=1)
    derived = sup.sum(axis=1, min_count=1)
    L.append(Line("uy_dem", "Inland consumption (= imports: no production or LNG)", DEMAND,
                  tuple(s for ln in L for s in ln.sources), derived,
                  "Derived in the workbook as total supply; blank when no supply is recorded",
                  formula="equals_total_supply"))
    return CountryBalance("Uruguay", L, idx, residual_label="Residual (zero by construction)")


# =========================================================================== flows by route
def flows(ctx: RunContext, bals: dict[str, CountryBalance]) -> list[Line]:
    idx = ctx.index
    out: list[Line] = []
    for sid, frm_to in (("enargas_imports", lambda cp: (cp, "Argentina")),
                        ("enargas_exp_dentro", lambda cp: ("Argentina", cp)),
                        ("enargas_exp_fuera", lambda cp: ("Argentina", cp))):
        sd = ctx.data(sid)
        if sd is None:
            out.append(Line(f"fl_{sid}", f"{sid}: all routes (source unavailable)", MEMO, (sid,), blank(idx)))
            continue
        for (route, cp), _ in sd.data.groupby(["route", "counterparty"]):
            a, b = frm_to(cp)
            out.append(Line(f"fl_{sid}_{route}", f"{a} -> {b}: {route}", MEMO, (sid,),
                            line_from(sd, idx, lambda d, r=route: d["route"] == r)))
    anp = ctx.data("anp")
    if anp is not None:
        pts = anp.data[(anp.data.measure == "Volume Realizado") & anp.data.category.isin(["bolivia_border", "uruguaiana",
                                                                                          "export"])]
        for (p, cat), _ in pts.groupby(["point", "category"]):
            a = "Bolivia" if cat == "bolivia_border" else ("Argentina" if cat == "uruguaiana" else "Brazil")
            b = "Brazil" if cat != "export" else "abroad"
            out.append(Line(f"fl_anp_{p}", f"{a} -> {b}: {p} (ANP)", MEMO, ("anp",),
                            line_from(anp, idx, lambda d, p=p: (d["point"] == p) & (d["measure"] == "Volume Realizado"))))
    else:
        out.append(Line("fl_anp", "Bolivia/Argentina -> Brazil border points (ANP unavailable)", MEMO, ("anp",),
                        blank(idx)))
    br = next((ln for ln in bals["Brazil"].lines if ln.key == "br_imp_bolivia"), None)
    if br is not None:
        out.append(Line("fl_bo_origin", "Bolivia -> Brazil: Bolivian-origin (derived)", MEMO, br.sources, br.series))
    kp = ctx.data("kpler")
    out.append(Line("fl_kpler", "LNG -> Chile (Kpler)", MEMO, ("kpler",), line_from(kp, idx)))
    return out


def build_all(ctx: RunContext) -> tuple[dict[str, CountryBalance], list[Line]]:
    out = {}
    for name, fn in (("Argentina", argentina), ("Brazil", brazil), ("Colombia", colombia), ("Bolivia", bolivia),
                     ("Chile", chile), ("Uruguay", uruguay)):
        out[name] = fn(ctx)
    fl = flows(ctx, out)
    _cross_checks(ctx, out)
    return out, fl


def _cross_checks(ctx: RunContext, bals: dict[str, CountryBalance]):
    for name, bal in bals.items():
        ends = {}
        for ln in bal.lines:
            if ln.side == MEMO:
                continue
            s = ln.series.dropna()
            if not s.empty:
                ends[ln.label] = s.index.max()
        if len(ends) > 1:
            lo, hi = min(ends.values()), max(ends.values())
            if (hi - lo).days > config.SOURCE_LAG_WARN_DAYS:
                late = [k for k, v in ends.items() if v == lo]
                bal.checks.append(Check(name, "timing mismatch: inputs end on different dates", "warn", lo,
                                        float((hi - lo).days), f"latest {hi.date()}, earliest {lo.date()} "
                                        f"({'; '.join(late[:3])}); residual after {lo.date()} reflects missing lines",
                                        ""))
        for ln in bal.lines:
            if ln.side != MEMO and "derived" in ln.label.lower() and (ln.series < 0).any():
                bal.checks.append(Check(name, "negative derived values (kept)", "warn", None,
                                        float((ln.series < 0).sum()), ln.label, ",".join(ln.sources)))
