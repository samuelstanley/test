import math

import pandas as pd

from latam_gas_snd.util import date_table, html_tables, merge_latest, norm, parse_date, parse_number


def test_parse_number_locales():
    assert parse_number("1.234,5") == 1234.5
    assert parse_number("1.234") == 1234          # decimal-comma locale: dot is thousands
    assert parse_number("12,75") == 12.75
    assert parse_number("1,234.5", decimal=".") == 1234.5
    assert parse_number("-3,2") == -3.2
    assert parse_number("(4,0)") == -4.0
    assert math.isnan(parse_number(""))
    assert math.isnan(parse_number("s/d"))


def test_parse_date_variants():
    assert parse_date("05/03/2021") == pd.Timestamp("2021-03-05")
    assert parse_date("2021-03-05") == pd.Timestamp("2021-03-05")
    assert parse_date("05-mar-21") == pd.Timestamp("2021-03-05")
    assert parse_date("05/03", default_year=2024) == pd.Timestamp("2024-03-05")
    assert parse_date("Total") is None
    assert parse_date("31/02/2021") is None


def test_norm_strips_accents():
    assert norm("  Bahía   Blanca ") == "bahia blanca"


def test_date_table_joins_header_rows():
    html = """<table>
      <tr><th>Fecha</th><th colspan=2>Chile</th><th>Total</th></tr>
      <tr><th></th><th>Gas Andes</th><th>Norandino</th><th></th></tr>
      <tr><td>01/03/2021</td><td>1.000,5</td><td>200</td><td>1.200,5</td></tr>
      <tr><td>02/03/2021</td><td>900</td><td></td><td>900</td></tr>
    </table>"""
    df, names = date_table(html_tables(html)[0])
    assert names == ["Chile | Gas Andes", "Chile | Norandino", "Total"]
    assert df.loc["2021-03-01", "Chile | Gas Andes"] == 1000.5
    assert math.isnan(df.loc["2021-03-02", "Chile | Norandino"])


def test_merge_latest_reports_revisions():
    df = pd.DataFrame({"date": ["d1", "d1", "d2"], "k": ["a", "a", "a"], "value": [1.0, 2.0, 3.0],
                       "run": ["r1", "r2", "r1"]})
    merged, rev = merge_latest(df, ["date", "k"])
    assert merged.set_index("date").loc["d1", "value"] == 2.0
    assert len(rev) == 1
