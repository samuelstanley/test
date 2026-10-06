import pandas as pd
import pytest

from latam_gas_snd.sources.chartpdf import detect_unit, parse_chart_pdf
from tests.chart_factory import make_chart

COLOURS = {"Gasoducto Norte": (0.1, 0.3, 0.8), "Gasoducto Sur": (0.9, 0.5, 0.1), "Total inyectado": (0.2, 0.7, 0.2)}


def _chart(start="2024-03-01", n=10, **kw):
    dates = list(pd.date_range(start, periods=n))
    a = [10 + (i % 4) for i in range(n)]
    b = [20 + (i % 5) for i in range(n)]
    t = [x + y for x, y in zip(a, b)]
    pdf = make_chart(dates, {"Gasoducto Norte": a, "Gasoducto Sur": b}, {"Total inyectado": t}, COLOURS, **kw)
    return pdf, dates, a, b, t


@pytest.mark.parametrize("rotate", [False, True])
def test_stacked_bars_and_line_read_exactly(rotate):
    pdf, dates, a, b, t = _chart(rotate_dates=rotate)
    res = parse_chart_pdf(pdf)
    assert res.warnings == []
    assert set(res.legend.label) == set(COLOURS)
    p = res.data.pivot_table(index="date", columns="series", values="value")
    assert list(p.index) == dates
    assert p["Gasoducto Norte"].round(6).tolist() == a
    assert p["Gasoducto Sur"].round(6).tolist() == b
    assert p["Total inyectado"].round(6).tolist() == t
    assert res.unit == "thousand m3"


def test_day_month_labels_roll_over_year_end():
    pdf, dates, *_ = _chart(start="2024-12-28", n=8, date_fmt="%d/%m", rotate_dates=True)
    res = parse_chart_pdf(pdf, default_year=2024)
    assert sorted(res.data.date.unique()) == dates


def test_page_without_axis_is_reported_not_guessed():
    from reportlab.pdfgen import canvas
    import io
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(100, 700, "Sin datos")
    c.save()
    res = parse_chart_pdf(buf.getvalue())
    assert res.data.empty
    assert any("axis" in w for w in res.warnings)


def test_detect_unit():
    assert detect_unit("Volumen en MMm3/d") == "mcm"
    assert detect_unit("miles de m3 por día") == "thousand m3"
    assert detect_unit("sin unidad") is None
