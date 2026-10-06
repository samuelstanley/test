"""Test-only: draw vector PDF charts with known values to exercise the chart parser.

These charts are synthetic test fixtures. They are never read by the pipeline.
"""
from __future__ import annotations

import io

import pandas as pd
from reportlab.lib.colors import Color
from reportlab.pdfgen import canvas

PAGE_W, PAGE_H = 842, 595
X0, Y0, PLOT_W, PLOT_H = 80, 120, 600, 380   # reportlab origin is bottom-left


def make_chart(dates: list[pd.Timestamp], bars: dict[str, list[float]], lines: dict[str, list[float]],
               colours: dict[str, tuple[float, float, float]], ymax: float = 100, ystep: float = 20,
               title: str = "Inyección por gasoducto (miles de m3/d)", date_fmt: str = "%d/%m/%Y",
               rotate_dates: bool = False) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    c.setFont("Helvetica", 12)
    c.drawString(X0, PAGE_H - 40, title)
    c.setFont("Helvetica", 8)

    def y_of(v: float) -> float:
        return Y0 + v / ymax * PLOT_H

    # gridlines and y ticks
    v = 0.0
    while v <= ymax + 1e-9:
        c.setStrokeColor(Color(0.85, 0.85, 0.85))
        c.line(X0, y_of(v), X0 + PLOT_W, y_of(v))
        c.drawRightString(X0 - 6, y_of(v) - 3, f"{v:,.0f}".replace(",", "."))
        v += ystep
    n = len(dates)
    slot = PLOT_W / n
    bw = slot * 0.6
    for i, d in enumerate(dates):
        xc = X0 + slot * (i + 0.5)
        label = d.strftime(date_fmt)
        if rotate_dates:
            c.saveState()
            c.translate(xc + 3, Y0 - 8)
            c.rotate(90)
            c.drawRightString(0, 0, label)
            c.restoreState()
        else:
            c.drawCentredString(xc, Y0 - 14, label)
        base = 0.0
        for name, vals in bars.items():
            val = vals[i]
            if val is None:
                continue
            c.setFillColor(Color(*colours[name]))
            c.rect(xc - bw / 2, y_of(base), bw, y_of(base + val) - y_of(base), stroke=0, fill=1)
            base += val
    for name, vals in lines.items():
        c.setStrokeColor(Color(*colours[name]))
        c.setLineWidth(1.5)
        p = c.beginPath()
        for i, val in enumerate(vals):
            xc = X0 + slot * (i + 0.5)
            (p.moveTo if i == 0 else p.lineTo)(xc, y_of(val))
        c.drawPath(p, stroke=1, fill=0)
    # legend
    lx, ly = X0, 60
    for name in list(bars) + list(lines):
        if name in bars:
            c.setFillColor(Color(*colours[name]))
            c.rect(lx, ly, 8, 8, stroke=0, fill=1)
        else:
            c.setStrokeColor(Color(*colours[name]))
            c.setLineWidth(2)
            c.line(lx, ly + 4, lx + 14, ly + 4)
        c.setFillColor(Color(0, 0, 0))
        c.drawString(lx + 17, ly + 1, name)
        lx += 150
    c.showPage()
    c.save()
    return buf.getvalue()
