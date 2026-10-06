"""Test-only fake web: deterministic responses shaped like each source, so the real
fetchers, balances and workbook writer can run end to end offline.

Every number here is synthetic test data and never reaches a real output.
"""
from __future__ import annotations

import io

import numpy as np
import pandas as pd

from latam_gas_snd import config, pbi
from latam_gas_snd.sources import anp, bmc, enargas, ons, xm
from tests.chart_factory import make_chart
from tests.test_enargas import Resp

WINDOW = pd.date_range("2021-01-01", "2021-01-14")


def _es(v: float) -> str:
    return f"{v:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")


PARTES = {
    "importaciones": ["GNL Escobar", "GNL Bahía Blanca", "Bolivia (YPFB)", "Chile (Gas Andes)"],
    "exp_dentro": ["Gas Andes", "Norandino", "Cruz del Sur", "Uruguaiana", "Por Bolivia (TGN)"],
    "exp_fuera": ["Methanex PAN", "Bandurria"],
}


def partes_value(tipo: str, route: str, i: int) -> float:
    return 1000.0 * (1 + PARTES[tipo].index(route)) + 10 * i


def partes_html(tipo: str, days) -> str:
    routes = PARTES[tipo]
    head = "<tr><th>Fecha</th>" + "".join(f"<th>{r}</th>" for r in routes) + "<th>Total</th></tr>"
    rows = ""
    for d in days:
        i = (d - WINDOW[0]).days
        vals = [partes_value(tipo, r, i) for r in routes]
        rows += f"<tr><td>{d:%d/%m/%Y}</td>" + "".join(f"<td>{_es(v)}</td>" for v in vals) + \
                f"<td>{_es(sum(vals))}</td></tr>"
    return ("<html><input id='fecha_desde'><input id='fecha_hasta'><script>var u='?fecha_desde='+a+'&fecha_hasta='+b;"
            f"</script><table>{head}{rows}</table></html>")


INJ = {"TGN Norte": [20.0] * 14, "TGS Neuba": [30.0] * 14, "GNL Escobar": [5.0] * 14}
DEM = {"Prioritaria": [25.0] * 14, "Industria": [15.0] * 14, "Usinas": [12.0] * 14, "GNC": [3.0] * 14}
LP_LEVEL = [200 + (i % 3) for i in range(14)]
COL = {"TGN Norte": (0.1, 0.3, 0.8), "TGS Neuba": (0.9, 0.5, 0.1), "GNL Escobar": (0.5, 0.1, 0.5),
       "Total": (0.2, 0.7, 0.2), "Prioritaria": (0.8, 0.1, 0.1), "Industria": (0.1, 0.6, 0.6),
       "Usinas": (0.6, 0.6, 0.1), "GNC": (0.3, 0.3, 0.3), "Line Pack": (0.0, 0.0, 0.9),
       "Mínimo operativo": (0.9, 0.0, 0.0)}


def chart_pdf(kind: str) -> bytes:
    dates = list(WINDOW)
    if kind == "inj":
        tot = [sum(v[i] for v in INJ.values()) for i in range(14)]
        return make_chart(dates, INJ, {"Total": tot}, COL, ymax=60, ystep=10,
                          title="Inyección por gasoducto (MMm3/d)", rotate_dates=True)
    if kind == "dem":
        tot = [sum(v[i] for v in DEM.values()) for i in range(14)]
        return make_chart(dates, DEM, {"Total": tot}, COL, ymax=60, ystep=10,
                          title="Demanda por segmento (MMm3/d)", rotate_dates=True)
    return make_chart(dates, {}, {"Line Pack": LP_LEVEL, "Mínimo operativo": [180] * 14}, COL, ymax=250, ystep=50,
                      title="Line pack del sistema (MMm3)", rotate_dates=True)


ANP_POINTS = [  # transporter, point, tipo de ponto, UF, value (thousand m3/d)
    ("TAG", "UPGN Cabiúnas", "Recebimento", "RJ", 40000.0),
    ("TAG", "Terminal de GNL Baía de Guanabara", "Recebimento", "RJ", 10000.0),
    ("TBG", "Corumbá", "Recebimento", "MS", 20000.0),
    ("TSB", "Uruguaiana", "Recebimento", "RS", 1500.0),
    ("TBG", "City Gate Campinas", "Entrega", "SP", 45000.0),
    ("TAG", "UTE Santa Cruz", "Entrega", "RJ", 15000.0),
    ("TAG", "REDUC", "Entrega", "RJ", 4000.0),
    ("TBG", "Ponto de Interconexão TBG/TAG", "Entrega", "SP", 8000.0),
    ("TAG", "Ponto de Interconexão TBG/TAG", "Recebimento", "SP", 8000.0),
    ("TBG", "Ponto Misterioso", "Entrega", "SP", 700.0),
]


def anp_csv(month: pd.Timestamp) -> bytes:
    days = pd.date_range(month, month + pd.offsets.MonthEnd(0))
    head = ["Mês/Ano", "Transportador", "Ponto", "Tipo de Ponto", "UF", "Tipo de Volume"] + [str(i) for i in range(1, 32)]
    lines = [";".join(head)]
    for tr, pt, tp, uf, v in ANP_POINTS:
        for meas in ("Volume Programado", "Volume Realizado"):
            vals = [_es(v * (1.1 if meas == "Volume Programado" else 1.0)) if d <= len(days) else ""
                    for d in range(1, 32)]
            lines.append(";".join([f"{month:%m/%Y}", tr, pt, tp, uf, meas] + vals))
    lines.append(";".join([f"{month:%m/%Y}", "TBG", "Empacotamento", "", "", "Empacotamento"] +
                          [_es(-100.0) if d <= len(days) else "" for d in range(1, 32)]))
    return "\n".join(lines).encode("latin-1")


def ons_parquet() -> bytes:
    ts = pd.date_range("2021-01-01", "2021-01-14 23:00", freq="h")
    df = pd.DataFrame({"din_instante": np.repeat(ts, 2), "nom_tipocombustivel": ["Gás", "Hidráulica"] * len(ts),
                       "nom_usina": ["UTE A", "UHE B"] * len(ts), "id_estado": ["RJ", "PR"] * len(ts),
                       "val_geracao": [500.0, 9999.0] * len(ts)})
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


BMC_FRAMES = {
    "bmc_injection": lambda: pd.DataFrame([{"Fecha": d, "Punto de entrada": p, "Energía (MBTU)": v}
                                           for d in WINDOW for p, v in (("Ballena", 300000.0), ("Cusiana", 600000.0),
                                                                       ("SPEC Cartagena", 200000.0))]),
    "bmc_imported_not_injected": lambda: pd.DataFrame([{"Fecha": d, "Comercializador": "X", "Cantidad MBTU": 50000.0}
                                                       for d in WINDOW]),
    "bmc_offtake_snt": lambda: pd.DataFrame([{"Fecha": d, "Sector de consumo": s, "Energía tomada MBTU": v}
                                             for d in WINDOW for s, v in (("Termoeléctrico", 250000.0),
                                                                          ("Industrial", 300000.0),
                                                                          ("Residencial", 350000.0),
                                                                          ("GNV", 60000.0), ("Refinería", 100000.0))]),
    "bmc_offtake_marketers": lambda: pd.DataFrame([{"Fecha": d, "Comercializador": "Y", "Energía MBTU": 1000000.0}
                                                   for d in WINDOW]),
    "bmc_offtake_tramo": lambda: pd.DataFrame([{"Fecha": d, "Transportador": "TGI", "Tramo": "Ballena-Barranca",
                                                "Energía MBTU": 900000.0} for d in WINDOW]),
}


def install(monkeypatch):
    """Patch every network call of every source with the fake web above."""
    def enargas_get(url, params=None, **kw):
        params = params or {}
        if url == config.ENARGAS_PARTES:
            return Resp(partes_html(params.get("tipo", "importaciones"), WINDOW[-3:]))
        if url == config.ENARGAS_CHART_ITEMS:
            name = {6: "inyeccion_por_gasoducto.pdf", 8: "demanda_por_segmento.pdf"}[int(params["cat"])]
            return Resp(f"<a href='archivos/{name}'>Gráfico</a>")
        if url == config.ENARGAS_CHARTS_PAGE:
            return Resp("<a href='archivos/linepack.pdf'>Line pack</a><a href='archivos/otro.pdf'>Otro</a>")
        if url.endswith(".pdf"):
            kind = "inj" if "inyeccion" in url else "dem" if "demanda" in url else "lp"
            r = Resp("")
            r.content = chart_pdf(kind)
            return r
        raise AssertionError(f"unexpected ENARGAS GET {url} {params}")

    def enargas_http(method, url, params=None, data=None, **kw):
        p = {**(params or {}), **(data or {})}
        if method == "GET" and "fecha_desde" in p:
            try:
                d0 = pd.to_datetime(p["fecha_desde"], format="%d/%m/%Y")
                d1 = pd.to_datetime(p["fecha_hasta"], format="%d/%m/%Y")
                return Resp(partes_html(p["tipo"], pd.date_range(max(d0, WINDOW[0]), min(d1, WINDOW[-1]))))
            except ValueError:
                pass
        return Resp(partes_html(p.get("tipo", "importaciones"), WINDOW[-3:]))

    monkeypatch.setattr(enargas, "get", enargas_get)
    monkeypatch.setattr(enargas, "http", enargas_http)
    monkeypatch.setattr(enargas.time, "sleep", lambda s: None)
    monkeypatch.setattr(enargas, "PROBE_WINDOW", (WINDOW[0].date(), WINDOW[4].date()))

    monkeypatch.setattr(anp, "get", lambda url, **kw: Resp(
        "<a href='/anp/arquivos/movimentacao-gasodutos-2021-01.csv'>Janeiro 2021 (CSV)</a>"
        "<a href='/anp/arquivos/movimentacao-gasodutos-2020-12.csv'>Dezembro 2020 (CSV)</a>"))
    monkeypatch.setattr(anp, "cached_download", lambda url, sid, refresh=False, **kw: anp_csv(
        pd.Timestamp("2021-01-01") if "2021-01" in url else pd.Timestamp("2020-12-01")))

    monkeypatch.setattr(ons, "list_keys", lambda: ["dataset/geracao_usina_2_ho/GERACAO_USINA-2_2021.parquet"])
    monkeypatch.setattr(ons, "cached_download", lambda url, sid, refresh=False, **kw: ons_parquet())

    index = "".join(f"<a href='/informacion-operativa/{i}'>{m['title']}</a>" for i, m in enumerate(
        config.BMC_REPORTS.values()))
    monkeypatch.setattr(bmc, "get", lambda url, **kw: Resp(
        index if url == config.BMC_INDEX else
        f"<iframe src='https://app.powerbi.com/view?r=ZmFrZQ%3D%3D&src={url}'></iframe>"))
    current = {}

    def load_report(view):
        page = view.split("src=")[-1]
        sid = next(s for s, m in config.BMC_REPORTS.items()
                   if (m["url"] and m["url"] == page) or page.endswith(str(list(config.BMC_REPORTS).index(s))))
        current["sid"] = sid
        return pbi.Report(view, "k", "api", 1, "ds", "rep", [pbi.Visual("Página 1", "v1", "tableEx", "", {}, [])])

    monkeypatch.setattr(pbi, "load_report", load_report)
    monkeypatch.setattr(pbi, "pick", lambda rep, kws=(), pin=None: rep.visuals[0])
    monkeypatch.setattr(pbi, "run_visual", lambda rep, v: BMC_FRAMES[current["sid"]]())

    def xm_post(url, json=None, **kw):
        d0, d1 = pd.Timestamp(json["StartDate"]), pd.Timestamp(json["EndDate"])
        items = [{"Date": f"{d:%Y-%m-%d}", "DailyEntities": [
            {"Id": "Recurso", "Values": {"code": "TSIE", "Combustible": "GAS", "Value": "100000"}},
            {"Id": "Recurso", "Values": {"code": "TERMOX", "Combustible": "ACPM", "Value": "5000"}}]}
            for d in pd.date_range(d0, d1) if d in WINDOW]
        r = Resp("")
        r.json = lambda: {"Items": items}
        return r
    monkeypatch.setattr(xm, "post", xm_post)
