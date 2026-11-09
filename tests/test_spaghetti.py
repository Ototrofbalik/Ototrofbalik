"""Plotly spaghetti / ısı haritası / önce-sonra / layout figürü testleri."""
import plotly.graph_objects as go
import pytest

from backend.demo.demo_factory import build_demo_project
from backend.layout.editing import move_workstation
from backend.routing.engine import compute_flow_metrics, compute_routes, derive_flow_edges
from backend.schemas import VehicleType
from backend.spaghetti.chart import (
    VEHICLE_COLOR,
    before_after_figure,
    heatmap_figure,
    layout_figure,
    spaghetti_figure,
)


@pytest.fixture(scope="module")
def demo():
    p = build_demo_project()
    edges = derive_flow_edges(p)
    rs = compute_routes(p.layout, edges)
    return p, rs, compute_flow_metrics(p.layout, edges, rs, p)


def route_traces(fig):
    return [t for t in fig.data if t.mode == "lines" and t.line.color in VEHICLE_COLOR.values()]


def test_spaghetti_colors_widths_arrows(demo):
    p, rs, m = demo
    fig = spaghetti_figure(p.layout, rs, m)
    assert isinstance(fig, go.Figure)
    rt = route_traces(fig)
    assert len(rt) == len(rs.routes) == 6
    assert {t.line.color for t in rt} == {VEHICLE_COLOR[VehicleType.OPERATOR], VEHICLE_COLOR[VehicleType.FORKLIFT]}
    assert len({t.line.width for t in rt}) == 1     # tüm frekanslar eşit (11/sa) -> eşit kalınlık
    arrows = [t for t in fig.data if t.marker is not None and t.marker.symbol == "arrow"]
    assert len(arrows) == 6
    assert "D_total 1540" in fig.layout.title.text
    assert len(fig.layout.shapes) >= 1 + len(p.layout.workstations) + len(p.layout.obstacles)


def test_width_proportional_to_frequency(demo):
    p, rs, m = demo
    cells = [c.model_copy(update={"moves_per_hour": 22.0 if c.from_id == "raw" else 11.0}) for c in m.cells]
    m2 = m.model_copy(update={"cells": cells})
    widths = {t.hovertext if False else t.text: t.line.width for t in route_traces(spaghetti_figure(p.layout, rs, m2))}
    wmax = max(widths.values())
    raw = [w for k, w in widths.items() if k.startswith("raw")][0]
    other = [w for k, w in widths.items() if not k.startswith("raw")][0]
    assert raw == wmax and raw > other


def test_vehicle_filter(demo):
    p, rs, m = demo
    fk = spaghetti_figure(p.layout, rs, m, vehicles={VehicleType.FORKLIFT})
    assert len(route_traces(fk)) == 4
    assert {t.line.color for t in route_traces(fk)} == {VEHICLE_COLOR[VehicleType.FORKLIFT]}
    assert len(route_traces(spaghetti_figure(p.layout, rs, m, vehicles={VehicleType.AGV}))) == 0


def test_euclidean_marked_dashed(demo):
    p, _, _ = demo
    edges = derive_flow_edges(p)
    rs = compute_routes(p.layout, edges, "euclidean")
    m = compute_flow_metrics(p.layout, edges, rs, p)
    fig = spaghetti_figure(p.layout, rs, m)
    assert all(t.line.dash == "dash" for t in route_traces(fig))
    assert "euclidean" in fig.layout.title.text


def test_unreachable_routes_not_drawn(demo):
    p, rs, m = demo
    broken = rs.model_copy(update={"routes": [rs.routes[0].model_copy(update={"reachable": False, "points": []})]
                                              + rs.routes[1:]})
    assert len(route_traces(spaghetti_figure(p.layout, broken, m))) == 5


def test_heatmap(demo):
    p, rs, m = demo
    fig = heatmap_figure(p.layout, rs, m)
    hm = [t for t in fig.data if isinstance(t, go.Heatmap)][0]
    flat = [v for row in hm.z for v in row if v is not None]
    assert flat and max(flat) >= 11.0
    assert max(flat) <= 11.0 * 6 + 1e-9


def test_before_after(demo):
    p, rs, m = demo
    lay2 = move_workstation(p.layout, "cut", 12, 2)    # akışa yakın bir yer (geçerlilik burada aranmıyor)
    edges = derive_flow_edges(p)
    rs2 = compute_routes(lay2, edges)
    m2 = compute_flow_metrics(lay2, edges, rs2, p)
    fig = before_after_figure((p.layout, rs, m), (lay2, rs2, m2))
    titles = [a.text for a in fig.layout.annotations]
    assert any("Önce" in t for t in titles) and any("Sonra" in t for t in titles)
    assert len(route_traces(fig)) == 12
    assert {t.xaxis for t in route_traces(fig)} == {"x", "x2"}


def test_layout_figure_marks_overlap_red():
    p = build_demo_project()
    ok = layout_figure(p.layout)
    assert "sorun yok" in ok.layout.title.text
    assert not any(s.line.color == "#d62728" for s in ok.layout.shapes)
    bad_layout = move_workstation(p.layout, "qc", 4, 14)
    bad = layout_figure(bad_layout)
    red = [s for s in bad.layout.shapes if s.line.color == "#d62728"]
    assert len(red) == 2    # asm + qc
    assert "kırmızı" in bad.layout.title.text


def test_png_export(demo):
    p, rs, m = demo
    fig = spaghetti_figure(p.layout, rs, m)
    try:
        png = fig.to_image(format="png", width=900, height=600)
    except Exception as exc:  # kaleido/Chrome ortamda yoksa dürüstçe atla
        pytest.skip(f"PNG dışa aktarımı bu ortamda çalışmıyor: {exc}")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
