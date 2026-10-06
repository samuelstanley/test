"""ENARGAS partes parsing, route mapping, Total reconciliation and date-range discovery.

The HTML here is a synthetic fixture shaped like the partes page (no <form>, date boxes
driven by script). It is test data only.
"""
import json
from datetime import date

import pandas as pd

from latam_gas_snd import config
from latam_gas_snd.model import RunContext
from latam_gas_snd.sources import enargas

PAGE = """<html><body>
<label>Fecha Desde</label><input type="text" id="fdesde" class="datepicker">
<label>Fecha Hasta</label><input type="text" id="fhasta" class="datepicker">
<button id="btnConsultar">Consultar</button> <a href="#" id="xls">Descargar .xls</a>
<script>
$('#btnConsultar').click(function(){
  var d = $('#fdesde').val(), h = $('#fhasta').val();
  window.location = 'dod-partes-exp-imp-consulta.php?tipo=' + tipo + '&fdesde=' + d + '&fhasta=' + h;
});
</script>
%TABLE%
</body></html>"""

ROUTES = ["Gas Andes", "Norandino", "Cruz del Sur", "Uruguaiana", "Methanex PAN", "Por Bolivia (TGN)"]


def table(days):
    head = "<tr><th>Fecha</th>" + "".join(f"<th>{r}</th>" for r in ROUTES) + "<th>Total</th></tr>"
    body = ""
    for i, d in enumerate(days):
        vals = [100 + i, 50, 0, 1000.5 if i % 2 else 0, 2000, 300]
        tot = sum(vals)
        cells = "".join(f"<td>{v:,.1f}</td>".replace(",", "X").replace(".", ",").replace("X", ".") for v in vals)
        body += f"<tr><td>{d:%d/%m/%Y}</td>{cells}<td>{tot:,.1f}</td></tr>".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"<table>{head}{body}</table>"


class Resp:
    def __init__(self, text):
        self.text = text
        self.content = text.encode("utf-8")


def fake_server(calls):
    def handler(method, url, params=None, data=None, **kw):
        calls.append((method, url, dict(params or {}), dict(data or {})))
        p = {**(params or {}), **(data or {})}
        if method == "GET" and "fdesde" in p and "fhasta" in p:
            try:
                d0 = pd.to_datetime(p["fdesde"], format="%d/%m/%Y")
                d1 = pd.to_datetime(p["fhasta"], format="%d/%m/%Y")
            except ValueError:
                return Resp(PAGE.replace("%TABLE%", table(pd.date_range("2026-01-01", "2026-01-05"))))
            return Resp(PAGE.replace("%TABLE%", table(pd.date_range(d0, d1))))
        # default view: current year only
        return Resp(PAGE.replace("%TABLE%", table(pd.date_range("2026-01-01", "2026-01-05"))))
    return handler


def test_parse_partes_and_route_mapping():
    html = PAGE.replace("%TABLE%", table(pd.date_range("2021-03-01", periods=3)))
    wide = enargas.parse_partes(html)
    assert list(wide.columns) == ROUTES + ["Total"]
    assert wide.loc["2021-03-02", "Uruguaiana"] == 1000.5
    long = enargas._partes_long(wide)
    sd = enargas._partes_source_data("enargas_exp_dentro", long.assign(run="x").drop(columns="run"),
                                     pd.DataFrame(), [])
    cp = sd.data.drop_duplicates("route").set_index("route")["counterparty"].to_dict()
    assert cp == {"Gas Andes": "Chile", "Norandino": "Chile", "Cruz del Sur": "Uruguay", "Uruguaiana": "Brazil",
                  "Methanex PAN": "Chile", "Por Bolivia (TGN)": "Brazil via Bolivia"}
    assert sd.data.loc[sd.data.route == "Methanex PAN", "methanex"].all()
    # acceptance: parsed routes equal the Total column every day
    summary = [c for c in sd.checks if "parsed routes vs ENARGAS Total" in c.check][0]
    assert summary.severity == "info" and summary.value == 0
    assert sd.data["value"].max() == 2000 / 1000     # thousand m3 -> mcm


def test_total_mismatch_is_flagged_not_fixed():
    wide = pd.DataFrame({"Gas Andes": [100.0], "Total": [150.0]}, index=pd.to_datetime(["2021-03-01"]))
    sd = enargas._partes_source_data("enargas_exp_dentro", enargas._partes_long(wide), pd.DataFrame(), [])
    errs = [c for c in sd.checks if c.severity == "error"]
    assert errs and errs[-1].value == -50
    assert sd.data["value"].sum() == 0.1          # value kept as published


def test_discovery_finds_script_driven_params_and_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(enargas, "http", fake_server(calls))
    monkeypatch.setattr(enargas, "get", lambda url, params=None, **kw: fake_server(calls)("GET", url, params))
    monkeypatch.setattr(enargas.time, "sleep", lambda s: None)
    ctx = RunContext(date(2021, 1, 1), date(2021, 12, 31))
    recipe = enargas._discover("importaciones", ctx)
    assert recipe is not None
    assert (recipe["from"], recipe["to"], recipe["fmt"], recipe["method"]) == ("fdesde", "fhasta", "%d/%m/%Y", "GET")
    cached = json.loads(config.ENARGAS_QUERY_CACHE.read_text())
    assert cached["from"] == "fdesde"
    # second call uses the cache: one verification query only
    calls.clear()
    assert enargas._discover("importaciones", ctx)["from"] == "fdesde"
    assert len(calls) == 1


def test_fetch_partes_full_history_by_year(monkeypatch):
    calls = []
    monkeypatch.setattr(enargas, "http", fake_server(calls))
    monkeypatch.setattr(enargas, "get", lambda url, params=None, **kw: fake_server(calls)("GET", url, params))
    monkeypatch.setattr(enargas.time, "sleep", lambda s: None)
    ctx = RunContext(date(2021, 1, 1), date(2022, 6, 30))
    sd = enargas.fetch_partes(ctx, "enargas_exp_dentro")
    assert sd.returned_dates.min() == pd.Timestamp("2021-01-01")
    assert sd.returned_dates.max() == pd.Timestamp("2022-06-30")
    assert len(sd.returned_dates) == len(pd.date_range("2021-01-01", "2022-06-30"))


def test_discovery_failure_falls_back_to_default_view(monkeypatch):
    def server(method, url, params=None, data=None, **kw):
        return Resp(PAGE.replace("%TABLE%", table(pd.date_range("2026-01-01", "2026-01-05"))))
    monkeypatch.setattr(enargas, "http", server)
    monkeypatch.setattr(enargas, "get", lambda url, params=None, **kw: server("GET", url, params))
    monkeypatch.setattr(enargas.time, "sleep", lambda s: None)
    ctx = RunContext(date(2021, 1, 1), date(2026, 1, 31))
    sd = enargas.fetch_partes(ctx, "enargas_imports")
    assert sd.returned_dates.min() == pd.Timestamp("2026-01-01")
    assert "default view only" in sd.notes[0]
    assert any(e.step == "enargas_discover" and e.status == "warn" for e in ctx.log)
