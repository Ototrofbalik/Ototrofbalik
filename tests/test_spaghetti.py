"""Plotly spaghetti / ısı haritası / önce-sonra / layout figürü testleri."""
import math

import plotly.graph_objects as go
import pytest

from backend.demo.demo_factory import build_demo_project
from backend.layout.editing import move_workstation
from backend.routing.engine import compute_flow_metrics, compute_routes, derive_flow_edges
from backend.schemas import Point, Route, VehicleType
from backend.spaghetti import chart as chart_mod
from backend.spaghetti.chart import (
    VEHICLE_COLOR,
    W_MAX,
    W_MIN,
    W_SINGLE,
    _offset_paths,
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


# --------------------------------------------------------------------------- okunabilirlik testleri
def _anns(fig, prefix):
    return [a for a in fig.layout.annotations if (a.name or "").startswith(prefix)]


def _dist_to_polyline(pt, pts):
    return min(chart_mod._pt_seg_dist(pt, a, b) for a, b in zip(pts, pts[1:], strict=False))


def test_offset_within_corridor_band(demo):
    p, rs, _ = demo
    paths = _offset_paths(list(rs.routes), p.layout)
    main = p.layout.corridors[0]
    poly = [(q.x, q.y) for q in main.points]
    ys = set()
    for i, path in paths.items():
        orig = rs.routes[i].points
        assert path[0] == (orig[0].x, orig[0].y) and path[-1] == (orig[-1].x, orig[-1].y)
        for a, b in zip(path, path[1:], strict=False):
            if abs(a[1] - b[1]) < 1e-9 and abs(a[1] - 10) < 1.5 and abs(a[0] - b[0]) > 1e-9:
                assert _dist_to_polyline(a, poly) <= 0.4 * main.width + 1e-6
                if min(a[0], b[0]) <= 10 and max(a[0], b[0]) >= 12:
                    ys.add(round(a[1], 3))
    assert len(ys) == 4    # 10-12 aralığını tam kat eden 4 rota (pack->ship x=12de başlar), hepsi ayrı şerit


def test_metrics_and_routes_untouched(demo):
    p, rs, m = demo
    before_r, before_m = rs.model_dump(), m.model_dump()
    fig = spaghetti_figure(p.layout, rs, m)
    assert rs.model_dump() == before_r and m.model_dump() == before_m
    assert m.total_distance_m_per_hour == pytest.approx(1540)
    texts = [t.text for t in route_traces(fig)]
    for r in rs.routes:
        assert any(f"{r.distance_m:.1f} m" in t for t in texts)


def test_move_labels_count(demo):
    p, rs, m = demo
    fig = spaghetti_figure(p.layout, rs, m)
    assert len(_anns(fig, "move:")) == 6 and len(_anns(fig, "move_table")) == 1
    fk = spaghetti_figure(p.layout, rs, m, vehicles={VehicleType.FORKLIFT})
    assert {a.name for a in _anns(fk, "move:")} == {"move:1", "move:2", "move:3", "move:6"}
    assert len(_anns(fk, "move_table")) == 1


def test_single_frequency_no_crash(demo):
    p, rs, m = demo
    fig = spaghetti_figure(p.layout, rs, m)
    assert {t.line.width for t in route_traces(fig)} == {W_SINGLE}
    assert any("eşit kalınlık" in (t.name or "") for t in fig.data)
    one = rs.model_copy(update={"routes": rs.routes[:1]})
    assert len(route_traces(spaghetti_figure(p.layout, one, m))) == 1
    empty = rs.model_copy(update={"routes": []})
    fig = spaghetti_figure(p.layout, empty, m)
    assert route_traces(fig) == [] and not any((t.name or "").startswith(("İnce", "Tüm")) for t in fig.data)


def test_width_range(demo):
    p, rs, m = demo
    cells = [c.model_copy(update={"moves_per_hour": 22.0 if c.from_id == "raw" else 11.0}) for c in m.cells]
    fig = spaghetti_figure(p.layout, rs, m.model_copy(update={"cells": cells}))
    ws = [t.line.width for t in route_traces(fig)]
    assert max(ws) == W_MAX and min(ws) == W_MIN


def test_station_labels_outside(demo):
    p, rs, m = demo
    fig = spaghetti_figure(p.layout, rs, m)
    assert not any(t.mode == "text" for t in fig.data)
    for s in p.layout.workstations:
        a = _anns(fig, f"station:{s.id}")[0]
        r = s.footprint()
        assert a.y in (r.y, r.y2)


def test_more_than_three_lanes(demo):
    lay = demo[0].layout
    routes = [Route(from_id=f"a{i}", to_id=f"b{i}", vehicle=VehicleType.OPERATOR, distance_m=20.0,
                    points=[Point(x=2, y=4), Point(x=2, y=10), Point(x=20, y=10), Point(x=20, y=4)])
              for i in range(6)]
    paths = _offset_paths(routes, lay)
    w = lay.corridors[0].width
    mids = []
    for path in paths.values():
        ys = [a[1] for a, b in zip(path, path[1:], strict=False) if abs(a[1] - b[1]) < 1e-9 and abs(a[0] - b[0]) > 5]
        assert ys and abs(ys[0] - 10) <= 0.4 * w + 1e-6
        mids.append(round(ys[0], 6))
    assert len(set(mids)) == 6


def test_one_way_corridor(demo):
    p, rs, m = demo
    cor = [c.model_copy(update={"one_way": True}) if c.id == "main" else c for c in p.layout.corridors]
    lay = p.layout.model_copy(update={"corridors": cor})
    fig = spaghetti_figure(lay, rs, m)
    arrows = [t for t in fig.data if t.marker is not None and t.marker.symbol == "arrow"]
    assert len(arrows) == 6
    for t, r in zip(arrows, rs.routes, strict=True):
        for ang, x, y in zip(t.marker.angle, t.x, t.y, strict=True):
            best = min(zip(r.points, r.points[1:], strict=False),
                       key=lambda ab: chart_mod._pt_seg_dist((x, y), (ab[0].x, ab[0].y), (ab[1].x, ab[1].y)))
            exp = math.degrees(math.atan2(best[1].x - best[0].x, best[1].y - best[0].y))
            assert abs((ang - exp + 180) % 360 - 180) < 1e-6
