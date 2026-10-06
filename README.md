# LatAm daily gas balance

Daily natural gas supply/demand balance, built from public historical data, for
**Argentina, Brazil, Colombia and Bolivia** plus the countries linked to them by pipeline
(**Chile, Uruguay**). Units are mcm/d. History starts in Jan 2021 where the source allows it.

## Run it

```bash
pip install -r requirements.txt            # Python 3.10+
python -m latam_gas_snd run                # 2021-01-01 to today -> output/
python -m latam_gas_snd run --start 2024-01-01 --end 2024-12-31
python -m latam_gas_snd run --off kpler,xm # switch sources off (their lines stay blank)
python -m latam_gas_snd run --only anp,ons # run only these sources
python -m latam_gas_snd sources            # list source ids
python -m latam_gas_snd.pbi <BMC page url> # inspect a Power BI dashboard and the visual auto-pick
python -m pytest                           # tests (synthetic fixtures, no network)
```

Chile LNG needs Kpler: `pip install kpler.sdk` and set `KPLER_EMAIL` / `KPLER_PASSWORD`.
Without them the line is blank and the Run log says why.

**Run it daily and keep `data/archive/`.** BMC (Colombia) dashboards show a rolling ~12 months,
so history only builds up from archived runs. ENARGAS chart PDFs are archived the same way.
`data/archive/` is git-ignored, so back it up or commit it on purpose.

## Outputs (`output/`)

| File | Contents |
|---|---|
| `latam_gas_snd_balance.xlsx` | About, one sheet per country, Flows by route, Checks, Sources, Coverage, Coverage by line, Run log |
| `latam_gas_snd_argentina.xlsx` | Balance, imports and exports by route, injection by pipeline, demand by sector (chart), linepack, chart legend review, route map, AR checks |
| `latam_gas_snd_brazil.xlsx` | Balance, by category, by category & state, by point, point list, BR checks, BR monthly |
| `latam_gas_snd_colombia.xlsx` | Balance, injection by entry point, offtake by sector, marketer and segment, imported not injected, XM burn by plant |
| `brazil_point_days.parquet` | Every ANP point-day, with its classification |

Country sheet layout: rows 1 to 6 are group, line, **source**, **frequency**, note and
**coverage** for each column; data starts on row 7. Inputs are values. **Total supply, Total
demand, Residual (supply less demand) and Missing inputs are live formulas.** Memo columns sit
to the right and are excluded from the totals.

## Rules the code enforces

1. **Daily for every country**: production, demand, imports and LNG, split into supply and demand.
2. **No manual inputs.** Every line comes from a fetcher. The only hand-edited file is
   `inputs/br_point_overrides.csv`, which classifies ANP points and holds no volumes.
3. **No dummy data.** A source that is off or fails, or returns nothing for a day, leaves its
   cells blank. Each source runs in isolation, so one failure blanks only the lines that depend
   on it. Nothing is back-solved or plugged. Strict by default: a failed source does not fall back
   to its archive (`config.USE_ARCHIVE_ON_FAILURE`).
4. **Flag, don't smooth.** Route totals that don't match, unmapped routes or points, negative
   derived values, timing mismatches and parser warnings go to the Checks sheets. Values are
   never adjusted.
5. **Provenance.** Source and frequency sit under every column. The Sources sheet gives the
   URL, unit and conversion, and this run's status. Coverage gives first date, last date and
   days with data per source and per line.

A tested acceptance rule: switching a source off blanks exactly the lines that use it, lists
it in the Run log, and leaves every other line identical.

## Balance structure and sources

| Country | Supply | Demand | Memo |
|---|---|---|---|
| Argentina | Production = injection into transport (ENARGAS chart cat=6, import/LNG-named series excluded) + producer-line exports (`exp_fuera`); pipeline imports Bolivia and Chile; LNG Escobar and Bahía Blanca (partes `importaciones`) | Priority, industry, power, CNG (chart cat=8); exports by counterparty, in system (`exp_dentro`) and producer lines (`exp_fuera`); linepack change (chart level, day-on-day) | ENARGAS Total columns, chart totals, linepack level |
| Brazil | ANP: production into grid, LNG, Bolivia border split into Bolivian-origin (derived) and Argentine-origin (ENARGAS exports "por Bolivia"), Uruguaiana, biomethane | ANP: power, refineries, fertiliser, city gates (industry, residential and CNG are inside), exports, Empacotamento | ANP border total, ENARGAS mirror of Uruguaiana, ONS gas-fired generation (gas equivalent), net of dropped interconnections |
| Colombia | BMC injection: production vs imports (SPEC regas); imported gas not injected into the SNT | BMC offtake by sector as published; imported gas used outside the SNT; XM burn only if BMC has no thermal split | XM gas burn, BMC marketer and segment totals |
| Bolivia | Production: **blank, no daily public source** | Domestic demand: **blank**; exports to Argentina (ENARGAS); exports to Brazil, Bolivian-origin (ANP border less Argentine transit) | Transit, ANP border |
| Chile | Pipeline imports from Argentina (ENARGAS export routes); LNG (Kpler only) | Methanex feedstock (ENARGAS Methanex routes); exports to Argentina | Residual = power, distribution and industry |
| Uruguay | Imports from Argentina (Cruz del Sur, PetroUruguay) | Inland consumption = supply (formula) | |

Conversions live in `latam_gas_snd/config.py`: ENARGAS thousand m3 / 1000; GBTU x 0.0283
(Colombian MBTU = million Btu = 1/1000 GBTU); LNG m3 x 585; LNG tonne x 1,360; ONS
MWh x 3.6 / 45% / 0.0389 GJ/m3.

## Code map

```
latam_gas_snd/
  config.py        paths, conversions, URLs, source registry, classification rules, BMC_PINS
  model.py         Line, SourceData, RunContext (isolated fetches, run log, checks)
  sources/
    enargas.py     partes (date-range discovery + cache), chart PDFs, linepack
    chartpdf.py    vector PDF chart reader (legend colour -> series, axis calibration)
    anp.py         ANP CSV listing/parsing, point classification, overrides
    ons.py         ONS S3 parquet -> gas-fired generation
    bmc.py         BMC reports via Power BI, archived every run
    xm.py          XM API ConsCombustibleMBTU
    kpler.py       Kpler Flows (Chile LNG)
  pbi.py           public Power BI client + DSR decoder + CLI
  balances.py      country balances, cross-border flows, cross checks
  excel.py         workbook writer (formulas, provenance rows)
  pipeline.py      orchestration
inputs/br_point_overrides.csv
tests/             unit tests + end-to-end run on a fake web (synthetic fixtures only)
```

## Verification status

The build environment's network policy blocked every source host except the ONS S3 bucket.
What has and hasn't been checked:

| Source | Checked against live data? | Notes |
|---|---|---|
| ONS (S3) | **Yes**, full history 2021 to now, 2,104 days | Dataset prefix `geracao_usina_2_ho` confirmed. Older files store values as strings (handled). 2021 averages 38.8 mcm/d gas equivalent, 2023 averages 11.5 |
| ENARGAS partes | No (host blocked) | Parser, route mapping, Total reconciliation and date-range discovery tested on fixtures shaped like the page |
| ENARGAS chart PDFs | No | Parser tested on generated vector charts (stacked bars, lines, rotated and dd/mm labels, year rollover) |
| ANP CSVs | No | Wide-by-day layout detected from headers and values; unit read from file or inferred from magnitude (flagged) |
| BMC Power BI | No | DSR decoder (repeat/null bitmasks, dictionaries, restart tokens) tested on a synthetic response |
| XM API | No | Daily and hourly payload shapes handled; fuel field found by name |
| Kpler | No credentials | Call matches the kpler-sdk 1.0.64 `Flows.get` signature (checked in the package source) |

## Open issues, in priority order

1. **ENARGAS date range (blocking history).** `enargas._discover` scans the page's inputs and
   scripts, tests parameter pairs x date formats x GET/POST against a historic window, and
   caches the winner in `data/enargas_query.json`. It has been tested only against a fixture.
   Run it against the live page. If the Run log says no recipe was found, read the page JS
   (Fecha Desde / Fecha Hasta / "Descargar .xls") and write the recipe into
   `data/enargas_query.json` (`url`, `method`, `from`, `to`, `fmt`, `extra`).
2. **ENARGAS chart PDFs.** Check `data/archive/*_review.csv` (legend colour to series, points
   read, unit detected) and that "chart series sum vs chart total" in Checks is clean. Image
   (non-vector) charts are reported, not parsed.
3. Verify the XM endpoint, entity and fuel field (Run log shows what was used), and the Kpler
   unit (CM is tried first, then tonnes).
4. **BMC visual auto-pick:** run `python -m latam_gas_snd.pbi <page url>` for each report and
   pin the right visual in `config.BMC_PINS`. Only the "Energía inyectada" page URL is known.
   The others are found by title on the BMC "información operativa" index page.
5. **ANP format:** confirm the column detection on a real file (Run log notes and BR checks)
   and add unmatched points to `inputs/br_point_overrides.csv`.
6. **MME Boletim reconciliation:** the "BR monthly" sheet has ANP monthly averages by
   category, with an empty column for the Boletim's grid balance. The Boletim isn't fetched yet.

## Data flags kept

ENARGAS shows Bolivian imports into Argentina through 2026 (roughly 1 to 5.5 mcm/d at points).
They are treated as physical Bolivian supply, and a standing check is raised each run: verify
with YPFB whether this is Bolivian-origin gas or a swap tied to Argentine exports via Bolivia.
