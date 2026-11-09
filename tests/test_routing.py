"""Rotalama + akış metriği testleri (el hesabı)."""
import pytest

from backend.demo.demo_factory import build_demo_project
from backend.layout.geometry import segment_interior_overlap
from backend.routing.engine import (
    UnreachableFlowError,
    compute_flow_metrics,
    compute_routes,
    derive_flow_edges,
)
from backend.schemas import (
    Corridor,
    Facility,
    FlowEdge,
    Layout,
    Obstacle,
    Point,
    Product,
    Rect,
    RouteStep,
    VehicleType,
    Workstation,
)

OP, FK = VehicleType.OPERATOR, VehicleType.FORKLIFT


def ws(id_, x, y):
    # terminaller ayak izi merkezinde (offset 0)
    return Workstation(id=id_, name=id_, x=x, y=y, width=2, length=2)


def simple_layout(b_y=6.0, corridors=None, obstacles=None) -> Layout:
    return Layout(
        id="L", name="L", facility=Facility(width=20, height=20),
        workstations=[ws("A", 2, 6), ws("B", 14, b_y)],
        corridors=corridors if corridors is not None else [Corridor(id="c", points=[Point(x=0, y=10), Point(x=20, y=10)])],
        obstacles=obstacles or [],
    )


def edge(a, b, v=OP, f=10.0):
    return FlowEdge(from_id=a, to_id=b, vehicle=v, frequency_per_hour=f)


# ---- derive_flow_edges -------------------------------------------------------
def test_derive_edges_demo():
    p = build_demo_project()
    edges = derive_flow_edges(p)
    assert [(e.from_id, e.to_id, e.vehicle) for e in edges] == [
        ("raw", "cut", FK), ("cut", "drill", FK), ("drill", "asm", FK),
        ("asm", "qc", OP), ("qc", "pack", OP), ("pack", "ship", FK)]
    assert all(e.frequency_per_hour == pytest.approx(11.0) for e in edges)


def test_derive_edges_batch_and_aggregation():
    p = build_demo_project()
    prod = p.products[0].model_copy(update={"batch_size": 4})
    prod2 = Product(id="b", name="b", arrival_rate_per_hour=6, batch_size=2,
                    route=[RouteStep(station_id="raw"), RouteStep(station_id="cut", transport_in=FK)])
    p2 = p.model_copy(update={"products": [prod, prod2]})
    edges = {(e.from_id, e.to_id, e.vehicle): e for e in derive_flow_edges(p2)}
    assert edges[("asm", "qc", OP)].frequency_per_hour == pytest.approx(11 / 4)
    e = edges[("raw", "cut", FK)]
    assert e.frequency_per_hour == pytest.approx(11 / 4 + 3)
    assert e.quantity_per_move == pytest.approx((11 / 4 * 4 + 3 * 2) / (11 / 4 + 3))


def test_derive_edges_uses_explicit_flow_edges():
    p = build_demo_project()
    custom = [edge("raw", "ship", FK, 3.0)]
    p2 = p.model_copy(update={"flow_edges": custom})
    out = derive_flow_edges(p2)
    assert len(out) == 1 and out[0].frequency_per_hour == 3.0


# ---- bilinen mesafeler -------------------------------------------------------
def test_network_distance_hand_calculated():
    lay = simple_layout()
    # A merkez (3,7) -> (3,10): 3 ; koridor 12 ; (15,10)->(15,7): 3  => 18
    rs = compute_routes(lay, [edge("A", "B"), edge("B", "A")])
    r = rs.get("A", "B", OP)
    assert r.reachable and r.mode == "network" and r.distance_m == pytest.approx(18.0)
    assert (r.points[0].x, r.points[0].y) == (3, 7) and (r.points[-1].x, r.points[-1].y) == (15, 7)
    assert rs.get("B", "A", OP).distance_m == pytest.approx(18.0)   # çift yönlü koridor


def test_euclid_vs_network_reported_separately():
    lay = simple_layout(b_y=3.0)    # B merkez (15,4)
    e = [edge("A", "B")]
    net = compute_routes(lay, e, "network").routes[0]
    euc = compute_routes(lay, e, "euclidean").routes[0]
    man = compute_routes(lay, e, "manhattan").routes[0]
    assert net.distance_m == pytest.approx(3 + 12 + 6)
    assert euc.distance_m == pytest.approx((12**2 + 3**2) ** 0.5)
    assert man.distance_m == pytest.approx(15.0)
    assert (net.mode, euc.mode, man.mode) == ("network", "euclidean", "manhattan")
    assert compute_routes(lay, e, "euclidean").mode == "euclidean"
    assert euc.distance_m < net.distance_m


def test_bad_mode_and_unknown_station():
    with pytest.raises(ValueError):
        compute_routes(simple_layout(), [edge("A", "B")], "teleport")
    with pytest.raises(KeyError):
        compute_routes(simple_layout(), [edge("A", "Z")])


def test_same_station_zero_distance():
    rs = compute_routes(simple_layout(), [edge("A", "A")])
    assert rs.routes[0].distance_m == 0.0 and rs.routes[0].reachable


# ---- kenar durumlar ----------------------------------------------------------
def test_obstacle_blocks_all_access_unreachable():
    wall = Obstacle(id="w", rect=Rect(x=-1, y=8.5, w=10, h=1.0))   # A'nın yukarı çıkışını kapatır
    lay = simple_layout(obstacles=[wall])
    rs = compute_routes(lay, [edge("A", "B"), edge("B", "A")])
    r = rs.get("A", "B", OP)
    assert not r.reachable and r.points == []
    assert ("A", "B", "operator") in rs.unreachable
    with pytest.raises(UnreachableFlowError):
        compute_flow_metrics(lay, [edge("A", "B")], rs)


def test_obstacle_forces_detour_stub():
    # küçük engel A'nın doğrudan yukarı stub'ını keser; L biçimli stub dolaşır, mesafe 18'den büyük
    ob = Obstacle(id="o", rect=Rect(x=2.5, y=8.5, w=1.0, h=1.0))
    rs = compute_routes(simple_layout(obstacles=[ob]), [edge("A", "B")])
    r = rs.get("A", "B", OP)
    assert r.reachable and r.distance_m > 18.0
    for a, b in zip(r.points, r.points[1:], strict=False):
        assert not segment_interior_overlap((a.x, a.y), (b.x, b.y), ob.rect)


def test_one_way_corridor():
    c = Corridor(id="c", points=[Point(x=0, y=10), Point(x=20, y=10)], one_way=True)
    rs = compute_routes(simple_layout(corridors=[c]), [edge("A", "B"), edge("B", "A")])
    assert rs.get("A", "B", OP).reachable and rs.get("A", "B", OP).distance_m == pytest.approx(18)
    assert not rs.get("B", "A", OP).reachable
    assert rs.unreachable == [("B", "A", "operator")]


def test_one_way_loop_gives_longer_return():
    c1 = Corridor(id="c1", points=[Point(x=0, y=10), Point(x=20, y=10)], one_way=True)
    link1 = Corridor(id="l1", points=[Point(x=20, y=10), Point(x=20, y=12)], one_way=True)
    c2 = Corridor(id="c2", points=[Point(x=20, y=12), Point(x=0, y=12)], one_way=True)
    rs = compute_routes(simple_layout(corridors=[c1, link1, c2]), [edge("A", "B"), edge("B", "A")])
    assert rs.get("A", "B", OP).distance_m == pytest.approx(18.0)
    # B (15,7) -> (15,12): 5 ; batıya 12 ; (3,12)->(3,7): 5
    assert rs.get("B", "A", OP).distance_m == pytest.approx(22.0)


def test_allowed_vehicles():
    c = Corridor(id="c", points=[Point(x=0, y=10), Point(x=20, y=10)], allowed_vehicles=[OP])
    rs = compute_routes(simple_layout(corridors=[c]), [edge("A", "B", OP), edge("A", "B", FK)])
    assert rs.get("A", "B", OP).reachable
    assert not rs.get("A", "B", FK).reachable
    assert rs.unreachable == [("A", "B", "forklift")]


def test_disconnected_corridors_unreachable():
    c1 = Corridor(id="c1", points=[Point(x=0, y=10), Point(x=8, y=10)])
    c2 = Corridor(id="c2", points=[Point(x=10, y=10), Point(x=20, y=10)])
    rs = compute_routes(simple_layout(corridors=[c1, c2]), [edge("A", "B")])
    assert not rs.get("A", "B", OP).reachable


def test_crossing_corridors_form_junction():
    c1 = Corridor(id="h", points=[Point(x=0, y=10), Point(x=8, y=10)])
    c2 = Corridor(id="v", points=[Point(x=5, y=2), Point(x=5, y=18)])
    c3 = Corridor(id="h2", points=[Point(x=5, y=4), Point(x=20, y=4)])
    lay = simple_layout(corridors=[c1, c2, c3])
    r = compute_routes(lay, [edge("A", "B")]).routes[0]
    assert r.reachable and r.distance_m > 0


# ---- metrikler ---------------------------------------------------------------
def test_flow_metrics_demo_hand_calculated():
    p = build_demo_project()
    edges = derive_flow_edges(p)
    rs = compute_routes(p.layout, edges)
    assert rs.unreachable == []
    d = {(r.from_id, r.to_id): r.distance_m for r in rs.routes}
    assert d == pytest.approx({("raw", "cut"): 31.0, ("cut", "drill"): 19.0, ("drill", "asm"): 28.0,
                               ("asm", "qc"): 18.0, ("qc", "pack"): 17.0, ("pack", "ship"): 27.0})
    m = compute_flow_metrics(p.layout, edges, rs, p)
    assert m.mode == "network"
    assert m.total_distance_m_per_hour == pytest.approx(11 * 140)
    assert m.forklift_m_per_hour == pytest.approx(11 * (31 + 19 + 28 + 27))
    assert m.operator_walking_m_per_hour == pytest.approx(11 * (18 + 17))
    assert m.agv_m_per_hour == 0
    assert m.transport_time_s_per_hour == pytest.approx(11 * 105 / 2.5 + 11 * 35 / 1.2)
    assert m.material_handling_cost_per_hour == pytest.approx(11 * 105 * 0.20 + 11 * 35 * 0.05)
    assert m.distance_per_unit_m == pytest.approx(140.0)
    assert len(m.cells) == 6 and sum(c.metres_per_hour for c in m.cells) == pytest.approx(1540)


def test_cost_none_without_project_cost():
    p = build_demo_project()
    p2 = p.model_copy(update={"cost": None})
    edges = derive_flow_edges(p2)
    rs = compute_routes(p2.layout, edges)
    assert compute_flow_metrics(p2.layout, edges, rs, p2).material_handling_cost_per_hour is None
    assert compute_flow_metrics(p2.layout, edges, rs).material_handling_cost_per_hour is None


def test_metrics_without_project_uses_default_speeds_and_entry_flow():
    p = build_demo_project()
    edges = derive_flow_edges(p)
    rs = compute_routes(p.layout, edges)
    m = compute_flow_metrics(p.layout, edges, rs)
    assert m.total_distance_m_per_hour == pytest.approx(1540)
    assert m.distance_per_unit_m == pytest.approx(140.0)   # giriş akışı = 11/sa


def test_euclid_and_network_metrics_differ_and_are_labeled():
    p = build_demo_project()
    edges = derive_flow_edges(p)
    net = compute_flow_metrics(p.layout, edges, compute_routes(p.layout, edges, "network"), p)
    euc = compute_flow_metrics(p.layout, edges, compute_routes(p.layout, edges, "euclidean"), p)
    assert (net.mode, euc.mode) == ("network", "euclidean")
    assert euc.total_distance_m_per_hour < net.total_distance_m_per_hour


def test_demo_all_flow_connections_reachable_and_clear_of_obstacles():
    p = build_demo_project()
    edges = derive_flow_edges(p)
    rs = compute_routes(p.layout, edges)
    assert len(rs.routes) == len(edges) and all(r.reachable for r in rs.routes)
    for r in rs.routes:
        for a, b in zip(r.points, r.points[1:], strict=False):
            for o in p.layout.obstacles:
                assert not segment_interior_overlap((a.x, a.y), (b.x, b.y), o.rect)
            for s in p.layout.workstations:
                if s.id not in (r.from_id, r.to_id):
                    assert not segment_interior_overlap((a.x, a.y), (b.x, b.y), s.footprint())


def test_routes_deterministic():
    p = build_demo_project()
    edges = derive_flow_edges(p)
    a = compute_routes(p.layout, edges).model_dump()
    b = compute_routes(p.layout, edges).model_dump()
    assert a == b
