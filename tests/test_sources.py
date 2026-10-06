import base64
import json

import pandas as pd
import pytest

from latam_gas_snd import config, pbi
from latam_gas_snd.sources import anp, bmc, xm
from tests import fakes


# --------------------------------------------------------------------------- ANP
def test_anp_parse_wide_csv_keeps_realised_and_linepack():
    df, info = anp.parse_csv(fakes.anp_csv(pd.Timestamp("2021-02-01")), "movimentacao-2021-02.csv")
    assert info["columns"]["measure"] == "Tipo de Volume"
    assert info["columns"]["direction"] == "Tipo de Ponto"
    assert set(df.measure) == {"Volume Realizado", "Empacotamento"}      # Programado dropped
    assert df.date.min() == pd.Timestamp("2021-02-01") and df.date.max() == pd.Timestamp("2021-02-28")
    cab = df[(df.point == "UPGN Cabiúnas") & (df.date == "2021-02-10")]
    assert cab.value_raw.tolist() == [40000.0]


def test_anp_unit_from_header():
    assert anp.unit_factor("Volume (mil m³/dia)") == pytest.approx(1 / 1000)
    assert anp.unit_factor("m³") == pytest.approx(1e-6)
    assert anp.unit_factor("sem unidade") is None


@pytest.mark.parametrize("point,transporter,direction,expected", [
    ("UPGN Cabiúnas", "TAG", "receipt", "production"),
    ("Terminal de GNL Baía de Guanabara", "TAG", "receipt", "lng"),
    ("Corumbá", "TBG", "receipt", "bolivia_border"),
    ("Uruguaiana", "TSB", "receipt", "uruguaiana"),
    ("Uruguaiana", "TSB", "delivery", "export"),
    ("UTE Santa Cruz", "TAG", "delivery", "power"),
    ("REDUC", "TAG", "delivery", "refinery"),
    ("FAFEN-BA", "TAG", "delivery", "fertiliser"),
    ("City Gate Campinas", "TBG", "delivery", "city_gate"),
    ("Ponto de Interconexão TBG/TAG", "TBG", "delivery", "interconnection"),
    ("Biometano Gás Verde", "TAG", "receipt", "biomethane"),
    ("Ponto Misterioso", "TBG", "delivery", "unclassified"),
])
def test_anp_classification(point, transporter, direction, expected):
    assert anp.classify(point, transporter, direction, pd.DataFrame())[0] == expected


def test_anp_overrides_win(tmp_path, monkeypatch):
    p = tmp_path / "ov.csv"
    p.write_text("# comment\npoint,transporter,category,state,note\nPonto Misterioso,,city_gate,SP,checked\n")
    ov = anp.load_overrides(p)
    assert anp.classify("Ponto Misterioso", "TBG", "delivery", ov) == ("city_gate", "override")


def test_file_month():
    assert str(anp.file_month("movimentacao-gasodutos-2023-07.csv")) == "2023-07-01"
    assert str(anp.file_month("Julho 2023 (CSV)")) == "2023-07-01"


# --------------------------------------------------------------------------- Power BI
def test_pbi_decode_key():
    r = base64.b64encode(json.dumps({"k": "abc", "t": "tenant", "c": 5}).encode()).decode().rstrip("=")
    assert pbi.decode_key(f"https://app.powerbi.com/view?r={r}") == {"k": "abc", "t": "tenant", "c": 5}


def test_pbi_dsr_repeat_null_and_dictionaries():
    resp = {"results": [{"result": {"data": {
        "descriptor": {"Select": [{"Kind": 1, "Value": "G0", "Name": "Tabla.Fecha"},
                                  {"Kind": 1, "Value": "G1", "Name": "Tabla.Punto"},
                                  {"Kind": 2, "Value": "M0", "Name": "Sum(Tabla.Energia MBTU)"}]},
        "dsr": {"DS": [{"PH": [{"DM0": [
            {"S": [{"N": "G0", "T": 7}, {"N": "G1", "T": 1, "DN": "D0"}, {"N": "M0", "T": 4}],
             "C": [1614556800000, 0, 1000]},
            {"C": [1, 2000], "R": 1},
            {"C": [1614643200000, 0], "Ø": 4},
            {"C": [1614643200000, 1, 1500]},
        ]}], "ValueDicts": {"D0": ["Ballena", "SPEC"]}, "RT": [["x"]]}]}}}}]}
    names, rows, rt = pbi.decode_dsr(resp)
    assert names == ["Tabla.Fecha", "Tabla.Punto", "Sum(Tabla.Energia MBTU)"]
    assert rows == [[pd.Timestamp("2021-03-01"), "Ballena", 1000], [pd.Timestamp("2021-03-01"), "SPEC", 2000],
                    [pd.Timestamp("2021-03-02"), "Ballena", None], [pd.Timestamp("2021-03-02"), "SPEC", 1500]]
    assert rt == [["x"]]


def test_pbi_pick_prefers_dated_tables_and_honours_pins():
    sel = lambda *names: {"Select": [{"Name": n, **({"Aggregation": {}} if "Sum" in n else {"Column": {}})}  # noqa: E731
                                     for n in names]}
    rep = pbi.Report("u", "k", "api", 1, "ds", "r", [
        pbi.Visual("P", "card1", "card", "", sel("Sum(T.Energia)"), ["Sum(T.Energia)"]),
        pbi.Visual("P", "chart1", "lineChart", "", sel("T.Fecha", "Sum(T.Energia)"), ["T.Fecha", "Sum(T.Energia)"]),
        pbi.Visual("P", "tab1", "tableEx", "Energía inyectada", sel("T.Fecha", "T.Punto", "Sum(T.Energia)"),
                   ["T.Fecha", "T.Punto", "Sum(T.Energia)"]),
    ])
    assert pbi.pick(rep, ["inyect"]).id == "tab1"
    assert pbi.pick(rep, [], {"page": "P", "visual": "chart1"}).id == "chart1"
    with pytest.raises(ValueError):
        pbi.pick(rep, [], {"visual": "nope"})


def test_report_urls_found_in_iframe():
    html = '<iframe src="https://app.powerbi.com/view?r=eyJrIjoiYSJ9&amp;pageName=X"></iframe>'
    assert pbi.report_urls(html) == ["https://app.powerbi.com/view?r=eyJrIjoiYSJ9&pageName=X"]


# --------------------------------------------------------------------------- BMC / XM
def test_bmc_normalise_units_and_dims():
    df = fakes.BMC_FRAMES["bmc_injection"]()
    out, notes = bmc.normalise(df)
    assert set(out.columns) >= {"date", "Punto de entrada", "value_raw", "value", "unit"}
    assert out.unit.unique().tolist() == ["MBTU"]
    assert out.value.iloc[0] == pytest.approx(out.value_raw.iloc[0] * 0.0283 / 1000)


def test_bmc_normalise_builds_date_from_parts():
    df = pd.DataFrame({"Año": [2024, 2024], "Mes": ["enero", "febrero"], "Día": [5, 6], "Sector": ["Industrial"] * 2,
                       "Energía GBTU": [10.0, 11.0]})
    out, notes = bmc.normalise(df)
    assert out.date.tolist() == [pd.Timestamp("2024-01-05"), pd.Timestamp("2024-02-06")]
    assert out.unit.iloc[0] == "GBTU" and out.value.iloc[0] == pytest.approx(0.283)


def test_xm_hourly_records_summed():
    payload = {"Items": [{"Date": "2024-01-01", "HourlyEntities": [
        {"Id": "Recurso", "Values": {"code": "TSIE", "Combustible": "Gas Natural",
                                     **{f"Hour{h:02d}": "10" for h in range(1, 25)}}}]}]}
    recs = xm._records(payload)
    assert recs[0]["code"] == "TSIE" and xm._value(recs[0]) == 240


def test_conversions():
    assert config.MBTU_TO_MCM * 1e6 == pytest.approx(28.3)          # 1 million MBTU = 1,000 GBTU = 28.3 mcm
    assert config.ons_mwh_to_mcm(1e6) == pytest.approx(1e6 * 3.6 / 0.45 / 0.0389 / 1e6)
    assert config.LNG_M3_TO_GAS_M3 == 585 and config.LNG_TONNE_TO_GAS_M3 == 1360
