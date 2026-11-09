"""[QA] Kontrol 2 — bağımsız el hesabı: D_total, Little yasası, kapasite, seed tekrarlanabilirliği, CRN.

Beklenen değerler üretim kodundan değil, ELLE (burada yeniden yazılmış basit geometri / olay logu tekrar oynatma)
hesaplanır.
"""
from __future__ import annotations

import math
from collections import defaultdict

import pytest

from backend.analytics.bottleneck import compare_results
from backend.routing.engine import compute_flow_metrics, compute_routes, derive_flow_edges
from backend.schemas import EventType, ResourceConfig, VehicleType
from backend.simulation.engine import run_simulation

MAIN_Y = 10.0   # demo 'main' koridoru y=10


def _terminal(ws, kind: str) -> tuple[float, float]:
    c = ws.center()
    off = ws.output_offset if kind == "out" else ws.input_offset
    return c.x + off.x, c.y + off.y


def manual_distance(layout, a: str, b: str) -> float:
    """Demo geometrisinde el hesabı: çıkış noktasından ana koridora dik, koridorda yatay, giriş noktasına dik."""
    ox, oy = _terminal(layout.station(a), "out")
    ix, iy = _terminal(layout.station(b), "in")
    return abs(oy - MAIN_Y) + abs(ox - ix) + abs(iy - MAIN_Y)


# ----------------------------------------------------------------------------------------- D_total
def test_dtotal_hand_calculation_demo(demo_project):
    lay = demo_project.layout
    legs = [("raw", "cut"), ("cut", "drill"), ("drill", "asm"), ("asm", "qc"), ("qc", "pack"), ("pack", "ship")]
    hand = [manual_distance(lay, a, b) for a, b in legs]
    assert hand == [31.0, 19.0, 28.0, 18.0, 17.0, 27.0]
    assert sum(hand) == 140.0
    assert 140.0 * 11 == 1540.0

    edges = derive_flow_edges(demo_project)
    routes = compute_routes(lay, edges)
    flow = compute_flow_metrics(lay, edges, routes, demo_project)
    assert flow.distance_per_unit_m == pytest.approx(140.0)
    assert flow.total_distance_m_per_hour == pytest.approx(1540.0)
    for (a, b), h in zip(legs, hand, strict=True):
        r = next(x for x in routes.routes if x.from_id == a and x.to_id == b)
        assert r.distance_m == pytest.approx(h)
        # polyline uzunluğu == rapor edilen mesafe
        pl = sum(math.dist(p.as_tuple(), q.as_tuple()) for p, q in zip(r.points, r.points[1:], strict=False))
        assert pl == pytest.approx(r.distance_m)
    # araç kırılımı: operatör asm->qc (18) + qc->pack (17) = 35 m/ürün * 11 = 385; forklift 105*11 = 1155
    assert flow.operator_walking_m_per_hour == pytest.approx(385.0)
    assert flow.forklift_m_per_hour == pytest.approx(1155.0)
    assert flow.agv_m_per_hour == 0.0
    assert sum(c.metres_per_hour for c in flow.cells) == pytest.approx(flow.total_distance_m_per_hour)


def test_flow_cells_frequencies_equal_arrival_rate(demo_project):
    edges = derive_flow_edges(demo_project)
    assert len(edges) == 6 and {e.frequency_per_hour for e in edges} == {11.0}


# ----------------------------------------------------------------------------------------- Little yasası (olay logundan)
def _entity_times(sim):
    created, done = {}, {}
    for e in sim.events:
        if e.type == EventType.ENTITY_CREATED:
            created[e.entity_id] = e.t
        elif e.type == EventType.ENTITY_COMPLETED:
            done[e.entity_id] = e.t
    return created, done


def test_littles_law_from_event_log(analysis):
    sim = analysis.baseline_sim
    T = sim.config.duration_s
    assert sim.config.warmup_s == 0.0 and sim.config.replications == 1
    created, done = _entity_times(sim)
    assert len(sim.events) < 100_000     # kesilmemiş log
    # L = (1/T) * toplam sistemde kalma süresi (tamamlanmayanlar T'de kesilir) -> ortalama WIP
    L = sum(min(done.get(i, T), T) - c for i, c in created.items()) / T
    assert sim.kpis.avg_wip == pytest.approx(L, rel=1e-6)
    # lambda * W: tamamlanan ürünler için W = ortalama çevrim süresi
    comp = [done[i] - created[i] for i in done if done[i] <= T]
    assert len(comp) == sim.kpis.completed
    assert sim.kpis.avg_cycle_time_s == pytest.approx(sum(comp) / len(comp), rel=1e-9)
    assert sim.kpis.throughput_per_hour == pytest.approx(len(comp) / (T / 3600.0))


def test_littles_law_exact_in_hand_scenario(demo_project):
    """Deterministik varış: L = lambda * W tam sağlanır (açık form)."""
    sim, expected = _run_hand_scenario(demo_project)
    k = sim.kpis
    assert k.completed == expected["completed"] == 48
    assert k.throughput_per_hour == pytest.approx(6.0)
    assert k.avg_cycle_time_s == pytest.approx(164.0)          # 60 + (35/2.5 + 15 + 15) + 60
    assert k.avg_wip == pytest.approx(48 * 164.0 / 28800.0, rel=1e-9)
    # Little: L = lambda W, lambda = 48 / 28800 1/s
    assert k.avg_wip == pytest.approx((48 / 28800.0) * k.avg_cycle_time_s, rel=1e-9)
    assert k.avg_queue_wait_s == 0.0 and k.max_queue_len == 0
    assert k.total_travel_distance_m == pytest.approx(48 * 35.0)


def _run_hand_scenario(demo_project):
    """raw -> ship (forklift) tek rota, 6 ürün/saat deterministik varış (600 s aralık, ilk varış t=0)."""
    from backend.schemas import Product, RouteStep

    prod = Product(id="hand", name="El hesabı", arrival_rate_per_hour=6.0,
                   route=[RouteStep(station_id="raw"), RouteStep(station_id="ship", transport_in=VehicleType.FORKLIFT)])
    cfg = demo_project.sim_config.model_copy(update={"arrival": "deterministic"})
    proj = demo_project.model_copy(update={"products": [prod], "sim_config": cfg}, deep=True)
    assert manual_distance(proj.layout, "raw", "ship") == 35.0
    routes = compute_routes(proj.layout, derive_flow_edges(proj))
    return run_simulation(proj, routes), {"completed": 48}


# ----------------------------------------------------------------------------------------- kapasite
def _intervals(sim):
    """(tip, anahtar) -> [(başlangıç, bitiş)] olay çiftlerinden."""
    open_p, open_t = {}, {}
    proc: dict[str, list] = defaultdict(list)
    trans: dict[str, list] = defaultdict(list)
    ops: dict[str, list] = defaultdict(list)
    for e in sim.events:
        if e.type == EventType.PROCESS_STARTED:
            open_p[(e.entity_id, e.station_id)] = e
        elif e.type == EventType.PROCESS_FINISHED:
            s = open_p.pop((e.entity_id, e.station_id))
            proc[e.station_id].append((s.t, e.t))
            if s.resource_id:
                ops[s.resource_id].append((s.t, e.t))
        elif e.type == EventType.TRANSPORT_STARTED:
            open_t[(e.entity_id, e.from_id, e.to_id)] = e
        elif e.type == EventType.TRANSPORT_COMPLETED:
            s = open_t.pop((e.entity_id, e.from_id, e.to_id))
            trans[s.vehicle.value].append((s.t, e.t))
            ops[s.resource_id].append((s.t, e.t))
    return proc, trans, ops


def _max_concurrent(iv):
    ev = sorted([(a, 1) for a, _ in iv] + [(b, -1) for _, b in iv], key=lambda x: (x[0], x[1]))  # bitiş önce
    cur = mx = 0
    for _, d in ev:
        cur += d
        mx = max(mx, cur)
    return mx


def test_capacity_never_exceeded_in_event_log(demo_project, analysis):
    sim = analysis.baseline_sim
    proc, trans, ops = _intervals(sim)
    caps = {w.id: w.capacity for w in demo_project.layout.workstations}
    for sid, iv in proc.items():
        assert _max_concurrent(iv) <= caps[sid], sid
    counts = {r.vehicle.value: r.count for r in demo_project.sim_config.resources}
    for v, iv in trans.items():
        assert _max_concurrent(iv) <= counts[v], v
    # tek bir kaynak kimliği (operator-1...) aynı anda iki iş yapamaz
    for rid, iv in ops.items():
        assert _max_concurrent(iv) <= 1, rid
    assert {"operator-1", "operator-2", "forklift-1"} >= set(ops)
    # assembly kapasitesi 2: gerçekten 2 paralel kullanılıyor mu (aşırı kısıtlayıcı model değil)
    assert _max_concurrent(proc["asm"]) in (1, 2)


def test_event_counts_consistent(analysis):
    sim = analysis.baseline_sim
    c = defaultdict(int)
    for e in sim.events:
        c[e.type] += 1
    created = c[EventType.ENTITY_CREATED]
    assert c[EventType.ENTITY_COMPLETED] == sim.kpis.completed
    assert created >= sim.kpis.completed
    assert c[EventType.PROCESS_STARTED] >= c[EventType.PROCESS_FINISHED]
    assert c[EventType.TRANSPORT_STARTED] >= c[EventType.TRANSPORT_COMPLETED]
    # her istasyonun işlenen sayısı bitmiş süreç sayısıyla uyumlu (<= başlayan)
    fin = defaultdict(int)
    for e in sim.events:
        if e.type == EventType.PROCESS_FINISHED:
            fin[e.station_id] += 1
    for s in sim.stations:
        assert s.processed == fin[s.station_id]
    # olay zamanları monoton
    ts = [e.t for e in sim.events]
    assert ts == sorted(ts)


def test_utilization_matches_busy_time(demo_project, analysis):
    sim = analysis.baseline_sim
    proc, _, _ = _intervals(sim)
    T = sim.config.duration_s
    for s in sim.stations:
        cap = next(w.capacity for w in demo_project.layout.workstations if w.id == s.station_id)
        busy = sum(min(b, T) - a for a, b in proc[s.station_id])
        # yarım kalan süreçler (başladı, bitmedi) kullanımda sayılır; setup dahil olabilir -> alt sınır
        assert s.utilization >= busy / (cap * T) - 1e-9
        assert 0.0 <= s.utilization <= 1.0 + 1e-9


# ----------------------------------------------------------------------------------------- seed
def test_same_seed_identical_and_different_seed_differs(demo_project):
    routes = compute_routes(demo_project.layout, derive_flow_edges(demo_project))
    a = run_simulation(demo_project, routes)
    b = run_simulation(demo_project, routes)
    assert a.model_dump() == b.model_dump()
    cfg = demo_project.sim_config.model_copy(update={"seed": 43})
    c = run_simulation(demo_project, routes, cfg)
    assert c.kpis != a.kpis and [e.t for e in c.events[:20]] != [e.t for e in a.events[:20]]


def test_replications_ci_contains_mean(demo_project):
    routes = compute_routes(demo_project.layout, derive_flow_edges(demo_project))
    cfg = demo_project.sim_config.model_copy(update={"replications": 3, "duration_s": 2 * 3600})
    r = run_simulation(demo_project, routes, cfg)
    assert len(r.replication_kpis) == 3
    ci = r.ci95["throughput_per_hour"]
    vals = [k.throughput_per_hour for k in r.replication_kpis]
    assert ci.n == 3 and ci.mean == pytest.approx(sum(vals) / 3)
    assert ci.lo <= ci.mean <= ci.hi


# ----------------------------------------------------------------------------------------- CRN
def test_crn_same_arrivals_baseline_vs_alternatives(analysis):
    base_created = [e.t for e in analysis.baseline_sim.events if e.type == EventType.ENTITY_CREATED]
    assert base_created
    for alt in analysis.optimization.alternatives:
        assert alt.sim is not None and alt.sim.events
        alt_created = [e.t for e in alt.sim.events if e.type == EventType.ENTITY_CREATED]
        assert alt_created == base_created, alt.id         # birebir aynı varış zamanları
        assert alt.sim.seed == analysis.baseline_sim.seed


def test_crn_arrivals_independent_of_processing_variability_and_resources(demo_project):
    routes = compute_routes(demo_project.layout, derive_flow_edges(demo_project))
    base = run_simulation(demo_project, routes)
    cfg2 = demo_project.sim_config.model_copy(update={
        "processing_variability": "triangular",
        "resources": [ResourceConfig(vehicle=VehicleType.OPERATOR, count=3, speed_mps=1.5),
                      ResourceConfig(vehicle=VehicleType.FORKLIFT, count=2, speed_mps=3.0)]})
    other = run_simulation(demo_project, routes, cfg2)

    def arr(s):
        return [e.t for e in s.events if e.type == EventType.ENTITY_CREATED]

    assert arr(base) == arr(other)
    assert other.kpis != base.kpis


def test_compare_results_flags_same_seed(analysis, demo_project):
    best = next(a for a in analysis.optimization.alternatives if a.id == analysis.optimization.best_alternative_id)
    cmp = compare_results(analysis.baseline_sim, best.sim, analysis.flow, best.flow, demo_project)
    assert cmp.same_seed and cmp.seed == 42
    d = {x.name: x for x in cmp.deltas}
    assert d["flow.total_distance_m_per_hour"].baseline == pytest.approx(1540.0)
    assert d["flow.total_distance_m_per_hour"].scenario == pytest.approx(best.flow.total_distance_m_per_hour)
    assert d["throughput_per_hour"].delta_abs == pytest.approx(
        best.sim.kpis.throughput_per_hour - analysis.baseline_sim.kpis.throughput_per_hour)
    # farklı seed -> same_seed False ve uyarı notu
    other_cfg = best.sim.config.model_copy(update={"seed": 99})
    other = best.sim.model_copy(update={"seed": 99, "config": other_cfg})
    cmp2 = compare_results(analysis.baseline_sim, other, analysis.flow, best.flow, demo_project)
    assert cmp2.same_seed is False
