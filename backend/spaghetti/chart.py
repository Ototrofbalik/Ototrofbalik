"""[Layout&Routing Engineer] Spaghetti Chart (Plotly).

Renk = araç tipi, çizgi kalınlığı = frekans (hareket/saat), ok = yön. Çakışmalar (layout_figure) kırmızı.
Not: Route frekans taşımaz; frekans FlowMetrics.cells (from,to) hücresinden okunur. Aynı (from,to) çiftinde
birden fazla araç varsa hücre toplamı her araç çizgisine yazılır (hover'da belirtilir).
"""
from __future__ import annotations

import math
from collections import Counter

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from backend.schemas import FlowMetrics, Layout, Route, RouteSet, ValidationIssue, VehicleType

VEHICLE_COLOR = {
    VehicleType.OPERATOR: "#1f77b4",
    VehicleType.FORKLIFT: "#ff7f0e",
    VehicleType.AGV: "#2ca02c",
}
VEHICLE_LABEL = {
    VehicleType.OPERATOR: "Operatör",
    VehicleType.FORKLIFT: "Forklift",
    VehicleType.AGV: "AGV",
}
_RED = "#d62728"
_STATION_FILL = "rgba(120,144,156,0.35)"
_STATION_LINE = "#455a64"


# --------------------------------------------------------------------------- yardımcılar
def _sp(row: int | None, col: int | None) -> dict:
    return {"row": row, "col": col} if row is not None else {}


def _draw_base(fig: go.Figure, layout: Layout, row: int | None = None, col: int | None = None,
               bad_ids: set[str] | None = None) -> None:
    bad_ids = bad_ids or set()
    sp = _sp(row, col)
    fac = layout.facility
    fig.add_shape(type="rect", x0=0, y0=0, x1=fac.width, y1=fac.height,
                  line={"color": "#37474f", "width": 2}, fillcolor="rgba(0,0,0,0)", layer="below", **sp)
    for c in layout.corridors:
        bad = c.id in bad_ids
        fig.add_trace(go.Scatter(
            x=[p.x for p in c.points], y=[p.y for p in c.points], mode="lines",
            line={"color": "rgba(214,39,40,0.35)" if bad else "rgba(158,158,158,0.30)", "width": max(c.width * 6, 6)},
            hoverinfo="text", text=f"Koridor {c.id} ({c.width:g} m{', tek yön' if c.one_way else ''})",
            showlegend=False, ), **sp)
    for o in layout.obstacles:
        r, bad = o.rect, o.id in bad_ids
        fig.add_shape(type="rect", x0=r.x, y0=r.y, x1=r.x2, y1=r.y2,
                      line={"color": _RED if bad else "#212121", "width": 2},
                      fillcolor="rgba(214,39,40,0.45)" if bad else "rgba(33,33,33,0.55)", **sp)
    xs, ys, txt = [], [], []
    for s in layout.workstations:
        r, bad = s.footprint(), s.id in bad_ids
        fig.add_shape(type="rect", x0=r.x, y0=r.y, x1=r.x2, y1=r.y2,
                      line={"color": _RED if bad else _STATION_LINE, "width": 3 if bad else 1.5},
                      fillcolor="rgba(214,39,40,0.40)" if bad else _STATION_FILL, **sp)
        c = s.center()
        xs.append(c.x)
        ys.append(c.y)
        txt.append(s.name)
    fig.add_trace(go.Scatter(x=xs, y=ys, mode="text", text=txt, textposition="middle center",
                             hoverinfo="skip", showlegend=False, textfont={"size": 11}, ), **sp)


def _style_axes(fig: go.Figure, layout: Layout, row: int | None = None, col: int | None = None) -> None:
    fac = layout.facility
    sp = _sp(row, col)
    anchor = "x" if (row is None or (row, col) == (1, 1)) else f"x{(col or 1)}"
    fig.update_xaxes(range=[-1, fac.width + 1], title_text="x (m)", constrain="domain", **sp)
    fig.update_yaxes(range=[-1, fac.height + 1], title_text="y (m)", scaleanchor=anchor, scaleratio=1, **sp)


def _cell_moves(metrics: FlowMetrics) -> dict[tuple[str, str], float]:
    return {(c.from_id, c.to_id): c.moves_per_hour for c in metrics.cells}


def _width(freq: float, fmax: float) -> float:
    return 2.0 + 8.0 * (freq / fmax) if fmax > 0 else 2.0


def _draw_routes(fig: go.Figure, routes: RouteSet, metrics: FlowMetrics, vehicles: set[VehicleType] | None,
                 row: int | None = None, col: int | None = None, legend: bool = True) -> int:
    sp = _sp(row, col)
    moves = _cell_moves(metrics)
    shown = [r for r in routes.routes if r.reachable and len(r.points) >= 2 and (vehicles is None or r.vehicle in vehicles)]
    pair_vehicles = Counter((r.from_id, r.to_id) for r in routes.routes)
    fmax = max((moves.get((r.from_id, r.to_id), 0.0) for r in shown), default=0.0)
    seen_vehicle: set[VehicleType] = set()
    for r in shown:
        f = moves.get((r.from_id, r.to_id), 0.0)
        color = VEHICLE_COLOR[r.vehicle]
        note = " (hücre toplamı; aynı çiftte birden fazla araç)" if pair_vehicles[(r.from_id, r.to_id)] > 1 else ""
        hover = (f"{r.from_id} → {r.to_id}<br>{VEHICLE_LABEL[r.vehicle]}<br>{f:g} hareket/sa{note}"
                 f"<br>{r.distance_m:.1f} m ({r.mode})<br>{f * r.distance_m:.0f} m/sa")
        first = r.vehicle not in seen_vehicle
        seen_vehicle.add(r.vehicle)
        fig.add_trace(go.Scatter(
            x=[p.x for p in r.points], y=[p.y for p in r.points], mode="lines",
            line={"color": color, "width": _width(f, fmax), "dash": "solid" if r.mode == "network" else "dash"},
            opacity=0.7, name=VEHICLE_LABEL[r.vehicle], legendgroup=r.vehicle.value,
            showlegend=bool(first and legend), hoverinfo="text", text=hover, ), **sp)
        _add_arrow(fig, r, color, sp)
    return len(shown)


def _add_arrow(fig: go.Figure, r: Route, color: str, sp: dict) -> None:
    """Rotanın en uzun parçasının ortasına yön oku (marker 'arrow')."""
    pts = r.points
    best = max(range(len(pts) - 1), key=lambda i: math.dist((pts[i].x, pts[i].y), (pts[i + 1].x, pts[i + 1].y)))
    a, b = pts[best], pts[best + 1]
    dx, dy = b.x - a.x, b.y - a.y
    if math.hypot(dx, dy) < 1e-9:
        return
    fig.add_trace(go.Scatter(
        x=[(a.x + b.x) / 2], y=[(a.y + b.y) / 2], mode="markers",
        marker={"symbol": "arrow", "size": 13, "color": color, "angleref": "up",
                "angle": math.degrees(math.atan2(dx, dy)), "line": {"color": "white", "width": 1}},
        hoverinfo="skip", showlegend=False, ), **sp)


# --------------------------------------------------------------------------- public API
def spaghetti_figure(layout: Layout, routes: RouteSet, metrics: FlowMetrics,
                     vehicles: set[VehicleType] | None = None, title: str = "") -> go.Figure:
    """Layout + istasyonlar + engeller + yönlü rota çizgileri (araç rengi, frekansla kalınlık, ok)."""
    fig = go.Figure()
    _draw_base(fig, layout)
    _draw_routes(fig, routes, metrics, vehicles)
    _style_axes(fig, layout)
    mode_note = "" if routes.mode == "network" else f" — {routes.mode} (gerçek koridor yolu DEĞİL)"
    fig.update_layout(
        title=title or f"Spaghetti Chart — {layout.name} — D_total {metrics.total_distance_m_per_hour:.0f} m/sa{mode_note}",
        legend={"orientation": "h", "y": -0.15}, plot_bgcolor="white", margin={"l": 50, "r": 20, "t": 60, "b": 60})
    return fig


def heatmap_figure(layout: Layout, routes: RouteSet, metrics: FlowMetrics) -> go.Figure:
    """Izgara hücresi başına geçen hareket/saat (her rota hücreye en fazla bir kez sayılır)."""
    g = layout.facility.grid
    nx_, ny_ = math.ceil(layout.facility.width / g), math.ceil(layout.facility.height / g)
    z = [[0.0] * nx_ for _ in range(ny_)]
    moves = _cell_moves(metrics)
    for r in routes.routes:
        if not r.reachable or len(r.points) < 2:
            continue
        f = moves.get((r.from_id, r.to_id), 0.0)
        cells: set[tuple[int, int]] = set()
        for a, b in zip(r.points, r.points[1:], strict=False):
            n = max(1, int(math.dist((a.x, a.y), (b.x, b.y)) / (g / 4)))
            for k in range(n + 1):
                x = a.x + (b.x - a.x) * k / n
                y = a.y + (b.y - a.y) * k / n
                i, j = min(int(x // g), nx_ - 1), min(int(y // g), ny_ - 1)
                if 0 <= i < nx_ and 0 <= j < ny_:
                    cells.add((i, j))
        for i, j in cells:
            z[j][i] += f
    zz = [[v if v > 0 else None for v in row] for row in z]
    fig = go.Figure()
    fig.add_trace(go.Heatmap(
        z=zz, x=[(i + 0.5) * g for i in range(nx_)], y=[(j + 0.5) * g for j in range(ny_)],
        colorscale="YlOrRd", colorbar={"title": "hareket/sa"}, hoverongaps=False,
        hovertemplate="x=%{x:.1f} m, y=%{y:.1f} m<br>%{z:g} hareket/sa<extra></extra>"))
    _draw_base(fig, layout)
    _style_axes(fig, layout)
    fig.update_layout(title=f"Akış yoğunluğu ısı haritası — {layout.name} ({routes.mode})",
                      plot_bgcolor="white", showlegend=False, margin={"l": 50, "r": 20, "t": 60, "b": 50})
    return fig


def before_after_figure(before: tuple[Layout, RouteSet, FlowMetrics], after: tuple[Layout, RouteSet, FlowMetrics]) -> go.Figure:
    b_l, b_r, b_m = before
    a_l, a_r, a_m = after
    d0, d1 = b_m.total_distance_m_per_hour, a_m.total_distance_m_per_hour
    delta = f"{(d1 - d0) / d0 * 100:+.1f}%" if d0 > 0 else "n/a"
    fig = make_subplots(rows=1, cols=2, subplot_titles=(
        f"Önce — {b_l.name}: {d0:.0f} m/sa", f"Sonra — {a_l.name}: {d1:.0f} m/sa ({delta})"))
    for col, (lay, rs, mt) in enumerate(((b_l, b_r, b_m), (a_l, a_r, a_m)), start=1):
        _draw_base(fig, lay, 1, col)
        _draw_routes(fig, rs, mt, None, 1, col, legend=(col == 1))
        _style_axes(fig, lay, 1, col)
    fig.update_layout(title="Önce / Sonra — taşıma mesafesi karşılaştırması", plot_bgcolor="white",
                      legend={"orientation": "h", "y": -0.15}, margin={"l": 50, "r": 20, "t": 80, "b": 60})
    return fig


def layout_figure(layout: Layout, issues: list[ValidationIssue] | None = None) -> go.Figure:
    """Yalnız layout görünümü; çakışmalar kırmızı. issues None ise validate_layout ile hesaplanır."""
    if issues is None:
        from backend.layout.validation import validate_layout
        issues = validate_layout(layout).issues
    bad = {i for iss in issues for i in iss.entity_ids}
    fig = go.Figure()
    _draw_base(fig, layout, bad_ids=bad)
    _style_axes(fig, layout)
    n = len(issues)
    fig.update_layout(
        title=f"Layout — {layout.name}" + (f" — {n} sorun (kırmızı)" if n else " — sorun yok"),
        plot_bgcolor="white", showlegend=False, margin={"l": 50, "r": 20, "t": 60, "b": 50})
    return fig
