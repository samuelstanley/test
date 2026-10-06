"""Run every source in isolation, build the balances, write the workbooks."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Callable

from . import balances, config, excel
from .model import RunContext, SourceData
from .sources import anp, bmc, enargas, kpler, ons, xm

FETCHERS: dict[str, Callable[[RunContext], SourceData]] = {
    "enargas_imports": enargas.fetch_imports,
    "enargas_exp_dentro": enargas.fetch_exp_dentro,
    "enargas_exp_fuera": enargas.fetch_exp_fuera,
    "enargas_injection": enargas.fetch_injection,
    "enargas_demand": enargas.fetch_demand,
    "enargas_linepack": enargas.fetch_linepack,
    "anp": anp.fetch,
    "ons": ons.fetch,
    **{sid: bmc.make_fetcher(sid) for sid in config.BMC_REPORTS},
    "xm": xm.fetch,
    "kpler": kpler.fetch,
}
assert set(FETCHERS) == set(config.SOURCES), "every source needs metadata and a fetcher"


def run(start: date = config.START_DATE, end: date | None = None, off=(), out_dir: Path | None = None,
        fetchers: dict[str, Callable] | None = None, drilldowns: bool = True) -> dict[str, Path]:
    end = end or date.today()
    out_dir = Path(out_dir or config.OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    unknown = set(off) - set(config.SOURCES)
    if unknown:
        raise ValueError(f"unknown source ids: {sorted(unknown)}; see `python -m latam_gas_snd sources`")
    ctx = RunContext(start, end, off)
    for sid, fn in (fetchers or FETCHERS).items():
        ctx.fetch(sid, fn)
    bals, flows = balances.build_all(ctx)
    paths: dict[str, Path] = {}
    extra = []
    anp_data = ctx.data("anp")
    if anp_data is not None:
        p = out_dir / config.BRAZIL_POINT_PARQUET
        df = anp_data.data.copy()
        for c in df.columns:
            if df[c].dtype == object:
                df[c] = df[c].astype("string")
        df.to_parquet(p, index=False)
        paths["brazil_points"] = p
        extra.append(("brazil point-days", f"{len(df)} rows saved to {p.name}"))
    if drilldowns:
        for country, fname in config.DRILLDOWN_WORKBOOKS.items():
            p = out_dir / fname
            excel.write_drilldown(p, ctx, bals[country])
            paths[country] = p
            extra.append((f"drill-down {country}", f"written {p.name}"))
    p = out_dir / config.MAIN_WORKBOOK
    excel.write_main(p, ctx, bals, flows, extra)
    paths["main"] = p
    run.last_context = ctx          # for callers/tests that want the in-memory result
    run.last_balances = bals
    return paths
