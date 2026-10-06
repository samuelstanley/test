"""Command line: ``python -m latam_gas_snd run|sources``."""
from __future__ import annotations

import argparse
from datetime import date

from . import config


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="latam_gas_snd", description="LatAm daily gas supply/demand balance")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="fetch every source and write the workbooks")
    r.add_argument("--start", type=date.fromisoformat, default=config.START_DATE)
    r.add_argument("--end", type=date.fromisoformat, default=None, help="default: today")
    r.add_argument("--off", default="", help="comma-separated source ids to switch off (their lines stay blank)")
    r.add_argument("--only", default="", help="comma-separated source ids to run; all others switched off")
    r.add_argument("--out", default=None, help=f"output folder (default {config.OUTPUT_DIR})")
    r.add_argument("--no-drilldowns", action="store_true")
    sub.add_parser("sources", help="list source ids")
    a = ap.parse_args(argv)
    if a.cmd == "sources":
        for sid, meta in config.SOURCES.items():
            print(f"{sid:28s} {meta['name']}")
        return
    from .pipeline import run
    off = {s for s in a.off.split(",") if s}
    if a.only:
        only = {s for s in a.only.split(",") if s}
        off |= set(config.SOURCES) - only
    paths = run(a.start, a.end, off, a.out, drilldowns=not a.no_drilldowns)
    ctx = run.last_context
    print(f"window {ctx.start.date()} to {ctx.end.date()}")
    for sid, res in ctx.results.items():
        days = len(res.data.returned_dates) if res.data is not None else 0
        print(f"  {sid:28s} {res.status:15s} {days:5d} days  {res.message[:110]}")
    for k, p in paths.items():
        print(f"wrote {k}: {p}")


if __name__ == "__main__":
    main()
