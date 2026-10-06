"""Read daily values out of vector PDF charts (ENARGAS 'gráficos de programación').

Method (colour-based label mapping):
1. Legend: small filled boxes (bar series) or short strokes (line series) with text
   immediately to their right give colour -> series label.
2. Y axis: a right-aligned column of numeric tick labels gives a linear map from
   page y to value (fit must be near-perfect, else the page is rejected).
3. X axis: date tick labels (upright or rotated) give a linear map from page x to day.
4. Bars: filled rectangles in a legend colour; value = axis(top) - axis(bottom), so
   stacked segments are read individually. Lines: vertices of strokes in a legend
   colour, one value per day.

Everything the parser decides is returned in ``legend`` and ``warnings`` so a human
can review it (written to ``data/archive/*_review.csv`` by the caller). Nothing is
smoothed or filled: days without a mark are simply absent.
"""
from __future__ import annotations

import io
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..util import norm, parse_date, parse_number

_NUM_RE = re.compile(r"^[-+]?\(?\d{1,3}(?:[.,\s]?\d{3})*(?:[.,]\d+)?\)?%?$|^[-+]?\d+(?:[.,]\d+)?$")


@dataclass
class ChartResult:
    data: pd.DataFrame                      # date, series, value, kind, page
    legend: pd.DataFrame                    # label, colour, kind, page, points, total
    unit: str | None
    title: str
    text: str
    warnings: list[str] = field(default_factory=list)


def _rgb(c) -> tuple[int, int, int] | None:
    if c is None:
        return None
    if isinstance(c, (int, float)):
        c = (c,)
    try:
        v = tuple(float(x) for x in c)
    except (TypeError, ValueError):
        return None
    if len(v) == 1:
        g = round(v[0] * 255)
        return (g, g, g)
    if len(v) == 3:
        return tuple(round(x * 255) for x in v)  # type: ignore[return-value]
    if len(v) == 4:
        cc, m, y, k = v
        return (round(255 * (1 - cc) * (1 - k)), round(255 * (1 - m) * (1 - k)), round(255 * (1 - y) * (1 - k)))
    return None


def _hex(c: tuple[int, int, int] | None) -> str:
    return "" if c is None else "#%02x%02x%02x" % c


def _close(a, b, tol=6) -> bool:
    return a is not None and b is not None and all(abs(x - y) <= tol for x, y in zip(a, b))


def _vc(o) -> float:
    return (o["top"] + o["bottom"]) / 2


def _hc(o) -> float:
    return (o["x0"] + o["x1"]) / 2


def detect_unit(text: str) -> str | None:
    t = norm(text)
    if re.search(r"mm ?m3|millones de m3|mmm3|mm m³|mmm³|millones de metros", t):
        return "mcm"
    if re.search(r"miles de m3|miles de m³|km3|dam3|mil m3|miles de metros|m3 ?x ?1\.?000|mm3", t):
        return "thousand m3"
    return None


def parse_chart_pdf(content: bytes, default_year: int | None = None) -> ChartResult:
    import pdfplumber

    frames, legends, warnings = [], [], []
    texts = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            texts.append(text)
            try:
                df, leg, warn = _parse_page(page, pno, default_year or _year_hint(text))
            except _Skip as exc:
                warnings.append(f"page {pno}: {exc}")
                continue
            frames.append(df)
            legends.append(leg)
            warnings.extend(f"page {pno}: {w}" for w in warn)
    full_text = "\n".join(texts)
    data = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["date", "series", "value", "kind", "page"])
    legend = pd.concat(legends, ignore_index=True) if legends else pd.DataFrame(
        columns=["label", "colour", "kind", "page", "points", "total"])
    title = next((ln.strip() for ln in full_text.splitlines() if re.search(r"[A-Za-z]{4}", ln)), "")
    return ChartResult(data, legend, detect_unit(full_text), title, full_text, warnings)


class _Skip(Exception):
    pass


def _year_hint(text: str) -> int | None:
    years = re.findall(r"\b(20[1-4]\d)\b", text)
    return int(Counter(years).most_common(1)[0][0]) if years else None


def _rect_like(obj) -> dict | None:
    """Filled closed paths drawn as curves -> rectangle dict if axis-aligned."""
    pts = obj.get("pts") or []
    if not obj.get("fill") or len(pts) < 4:
        return None
    xs = sorted({round(p[0], 1) for p in pts})
    ys = sorted({round(p[1], 1) for p in pts})
    if len(xs) == 2 and len(ys) == 2:
        return {"x0": xs[0], "x1": xs[1], "top": ys[0], "bottom": ys[1], "width": xs[1] - xs[0],
                "height": ys[1] - ys[0], "non_stroking_color": obj.get("non_stroking_color"), "fill": True}
    return None


def _parse_page(page, pno: int, default_year: int | None):
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False, extra_attrs=["upright"])
    rects = [r for r in page.rects if r.get("fill")]
    rects += [rr for c in page.curves if (rr := _rect_like(c))]
    strokes = [o for o in list(page.lines) + list(page.curves) if o.get("stroke", True) and o.get("pts")]
    warnings: list[str] = []

    # ---- y axis
    nums = [w for w in words if w.get("upright", True) and _NUM_RE.match(w["text"].strip())]
    groups: dict[int, list] = defaultdict(list)
    for w in nums:
        groups[round(w["x1"] / 3)].append(w)
    # Horizontal gridlines/ticks: tick labels are snapped to them, since a label's
    # text centre can sit a little off the tick it names.
    grid_ys = []
    for s in strokes:
        ys_ = [p[1] for p in s["pts"]]
        xs_ = [p[0] for p in s["pts"]]
        if max(ys_) - min(ys_) < 0.5 and max(xs_) - min(xs_) > 3:
            grid_ys.append((sum(ys_) / len(ys_), min(xs_), max(xs_)))
    for r in rects:
        if r["height"] < 1.5 and r["width"] > 3:
            grid_ys.append(((r["top"] + r["bottom"]) / 2, r["x0"], r["x1"]))

    def tick_y(w) -> float:
        y = _vc(w)
        near = [g for g in grid_ys if abs(g[0] - y) <= max(w["bottom"] - w["top"], 4) * 0.8
                and g[1] >= w["x1"] - 2 and g[1] <= w["x1"] + 30]
        return min(near, key=lambda g: abs(g[0] - y))[0] if near else y

    axes = []
    for g in groups.values():
        vals = {}
        for w in g:
            vals[round(tick_y(w), 2)] = parse_number(w["text"])
        if len(vals) < 3:
            continue
        y = np.array(list(vals.keys()))
        v = np.array(list(vals.values()))
        if np.isnan(v).any() or np.ptp(v) == 0:
            continue
        m, c = np.polyfit(y, v, 1)
        pred = m * y + c
        ss = ((v - v.mean()) ** 2).sum()
        r2 = 1 - ((v - pred) ** 2).sum() / ss if ss else 0
        if r2 > 0.999 and m < 0:
            axes.append({"x1": max(w["x1"] for w in g), "m": m, "c": c, "ymin": y.min(), "ymax": y.max(),
                         "n": len(vals)})
    if not axes:
        raise _Skip("no linear numeric y axis found")
    axes.sort(key=lambda a: a["x1"])
    ax = axes[0]
    if len(axes) > 1:
        warnings.append(f"{len(axes)} numeric axes found; values read on the left axis only")

    def yval(y: float) -> float:
        return ax["m"] * y + ax["c"]

    ytol = (ax["ymax"] - ax["ymin"]) * 0.05 + 2
    in_plot_y = lambda top, bottom: top >= ax["ymin"] - ytol and bottom <= ax["ymax"] + ytol  # noqa: E731

    # ---- legend
    def label_right_of(o) -> str | None:
        vc = _vc(o)
        h = max(o["bottom"] - o["top"], 4)
        cands = [w for w in words if w["x0"] >= o["x1"] - 0.5 and w["x0"] <= o["x1"] + 20
                 and abs(_vc(w) - vc) <= h / 2 + 3 and re.search(r"[A-Za-zÁÉÍÓÚáéíóúñÑ]", w["text"])]
        if not cands:
            return None
        cands.sort(key=lambda w: w["x0"])
        parts = [cands[0]]
        line = sorted([w for w in words if abs(_vc(w) - _vc(cands[0])) < 3 and w["x0"] > cands[0]["x0"]],
                      key=lambda w: w["x0"])
        for w in line:
            if w["x0"] - parts[-1]["x1"] > 6:
                break
            if any(s is not o and s["x0"] >= parts[-1]["x1"] - 1 and s["x1"] <= w["x0"] + 1
                   and abs(_vc(s) - vc) < 4 for s in swatch_objs):
                break
            parts.append(w)
        return " ".join(p["text"] for p in parts)

    swatch_objs = []
    for r in rects:
        if 2 <= r["width"] <= 25 and 2 <= r["height"] <= 25:
            swatch_objs.append({**r, "_kind": "bar", "_col": _rgb(r.get("non_stroking_color"))})
    for s in strokes:
        xs = [p[0] for p in s["pts"]]
        ys = [p[1] for p in s["pts"]]
        if 4 <= max(xs) - min(xs) <= 40 and max(ys) - min(ys) <= 2:
            swatch_objs.append({"x0": min(xs), "x1": max(xs), "top": min(ys) - 1, "bottom": max(ys) + 1,
                                "_kind": "line", "_col": _rgb(s.get("stroking_color")), "_src": s})
    legend: list[dict] = []
    used_swatches = []
    for s in swatch_objs:
        if s["_col"] is None:
            continue
        lab = label_right_of(s)
        if not lab:
            continue
        clash = [e for e in legend if _close(e["colour_rgb"], s["_col"], 2) and e["label"] != lab]
        if clash:
            warnings.append(f"colour {_hex(s['_col'])} used by '{clash[0]['label']}' and '{lab}'; ambiguous")
        if any(e["label"] == lab and e["kind"] == s["_kind"] for e in legend):
            continue
        legend.append({"label": lab, "colour_rgb": s["_col"], "kind": s["_kind"]})
        used_swatches.append(s)
    if not legend:
        raise _Skip("no legend (colour swatch + label) found")

    def legend_for(col, kind=None):
        best = [e for e in legend if _close(e["colour_rgb"], col, 3) and (kind is None or e["kind"] == kind)]
        if not best and kind is not None:
            best = [e for e in legend if _close(e["colour_rgb"], col, 3)]
        return best[0] if len(best) == 1 else None

    # ---- x axis (dates)
    date_marks = []
    for w in words:
        if not w.get("upright", True):
            continue
        d = parse_date(w["text"], default_year)
        if d is not None and _vc(w) > ax["ymax"] - 2:
            date_marks.append((_hc(w), d, round(_vc(w))))
    date_marks += _rotated_dates(page, default_year, ax["ymax"])
    if len(date_marks) < 2:
        raise _Skip("fewer than two date labels on the x axis")
    rows = Counter(r for _, _, r in date_marks)
    if len(rows) > 1 and all(isinstance(r, int) for r in rows):
        best_row = rows.most_common(1)[0][0]
        date_marks = [m for m in date_marks if m[2] == best_row or not isinstance(m[2], int)] or date_marks
    date_marks.sort(key=lambda m: m[0])
    # day/month labels without year: roll the year forward across December
    fixed, prev = [], None
    for x, d, _ in date_marks:
        while prev is not None and d < prev - pd.Timedelta(days=180):
            d = d + pd.DateOffset(years=1)
        fixed.append((x, d))
        prev = d
    xs = np.array([x for x, _ in fixed])
    ords = np.array([d.toordinal() for _, d in fixed], dtype=float)
    if np.ptp(ords) == 0:
        raise _Skip("x-axis date labels do not span more than one day")
    a, b = np.polyfit(ords, xs, 1)
    resid = np.abs((xs - b) / a - ords).max()
    if resid > 0.5:
        warnings.append(f"x-axis date labels not evenly spaced (max residual {resid:.2f} days)")
    xmin_plot = ax["x1"]

    def xdate(x: float) -> pd.Timestamp:
        return pd.Timestamp.fromordinal(int(round((x - b) / a)))

    # ---- bars
    recs = []
    seen_geo = set()
    for r in rects:
        if any(_close((r["x0"], r["top"], r["x1"], r["bottom"]), (s["x0"], s["top"], s["x1"], s["bottom"]), 0.5)
               for s in used_swatches if s["_kind"] == "bar"):
            continue
        col = _rgb(r.get("non_stroking_color"))
        e = legend_for(col, "bar")
        if e is None or r["height"] < 0.2 or r["x0"] < xmin_plot - 1 or not in_plot_y(r["top"], r["bottom"]):
            continue
        geo = (round(r["x0"], 1), round(r["top"], 1), round(r["x1"], 1), round(r["bottom"], 1), e["label"])
        if geo in seen_geo:
            continue
        seen_geo.add(geo)
        vt, vb = yval(r["top"]), yval(r["bottom"])
        if vt > 1e-9 and vb < -1e-9:
            warnings.append(f"bar for {e['label']} crosses zero at x={_hc(r):.0f}; read as top-bottom")
        value = vt - vb if vt > 1e-9 else vb - vt
        recs.append({"date": xdate(_hc(r)), "series": e["label"], "value": value, "kind": "bar"})
    # ---- lines
    plot_w = max(xs.max() - xmin_plot, 1)
    for s in strokes:
        if any(u.get("_src") is s for u in used_swatches):
            continue
        col = _rgb(s.get("stroking_color"))
        e = legend_for(col, "line")
        if e is None or e["kind"] != "line":
            continue
        pts = [(x, y) for x, y in s["pts"] if x >= xmin_plot - 1 and in_plot_y(y, y)]
        if not pts:
            continue
        ys = [p[1] for p in pts]
        if len(s["pts"]) == 2 and max(ys) - min(ys) < 0.01 and \
                (max(p[0] for p in pts) - min(p[0] for p in pts)) > 0.5 * plot_w:
            continue  # a single full-width horizontal segment is a gridline, not a daily series
        for x, y in pts:
            recs.append({"date": xdate(x), "series": e["label"], "value": yval(y), "kind": "line"})
    df = pd.DataFrame(recs, columns=["date", "series", "value", "kind"])
    if not df.empty:
        # bars: distinct segments of one series on one day are summed; lines: vertices averaged
        bars = df[df.kind == "bar"].groupby(["date", "series", "kind"], as_index=False)["value"].sum()
        lines = df[df.kind == "line"]
        if not lines.empty:
            spread = lines.groupby(["series", "date"])["value"].agg(lambda v: v.max() - v.min())
            if (spread > 1e-6 * max(1, lines.value.abs().max())).any():
                warnings.append("line series had several different vertices on one day; mean taken")
            lines = lines.groupby(["date", "series", "kind"], as_index=False)["value"].mean()
        df = pd.concat([bars, lines], ignore_index=True)
    df["page"] = pno
    leg = pd.DataFrame([{
        "label": e["label"], "colour": _hex(e["colour_rgb"]), "kind": e["kind"], "page": pno,
        "points": int((df.series == e["label"]).sum()) if not df.empty else 0,
        "total": float(df.loc[df.series == e["label"], "value"].sum()) if not df.empty else 0.0,
    } for e in legend])
    for _, r in leg.iterrows():
        if r["points"] == 0:
            warnings.append(f"legend entry '{r['label']}' ({r['colour']}) matched no marks")
    return df, leg, warnings


def _rotated_dates(page, default_year, y_axis_bottom) -> list:
    """Rebuild rotated tick labels from non-upright chars grouped by column."""
    chars = [c for c in page.chars if not c.get("upright", True) and _vc(c) > y_axis_bottom - 2]
    if not chars:
        return []
    cols: dict[int, list] = defaultdict(list)
    for c in chars:
        cols[round(_hc(c) / 2)].append(c)
    out = []
    for group in cols.values():
        # text matrix b > 0: rotated counter-clockwise, reads bottom to top
        b = [c.get("matrix", (0, 0))[1] for c in group]
        bottom_up = sum(1 for v in b if v > 0) >= sum(1 for v in b if v < 0)
        s = "".join(c["text"] for c in sorted(group, key=lambda c: c["top"], reverse=bottom_up)).strip()
        d = parse_date(s, default_year)
        if d is not None:
            out.append((float(np.mean([_hc(c) for c in group])), d, "rot"))
    return out
