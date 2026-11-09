"""SimPy motoru testleri. RouteSet routing modülünden bağımsız, elle kurulur (belgelenmiş mesafelerle)."""
from __future__ import annotations

import itertools

import pytest

from backend.demo.demo_factory import build_demo_project
from backend.schemas import (
    Corridor,
    EventType,
    Facility,
    Layout,
    Point,
    Product,
    Project,
    ResourceConfig,
    Route,
    RouteSet,
    RouteStep,
    SimConfig,
    VehicleType,
    Workstation,
)
from backend.simulation import engine
from backend.simulation.engine import RouteUnavailableError, SimulationError, run_simulation

OP, FK = VehicleType.OPERATOR, VehicleType.FORKLIFT
ET = EventType


# ------------------------------------------------------------------ yardımcılar
def manhattan_routes(project: Project, vehicle_dist: dict[VehicleType, float] | None = None) -> RouteSet:
    """Projedeki tüm ardışık geçişler için rota kurar. Mesafe: istasyon merkezleri arası Manhattan (m);
    `vehicle_dist` verilirse araç başına SABİT mesafe. Mod 'manhattan' (yaklaşık) olarak işaretlenir."""
    routes, seen = [], set()
    for p in project.products:
        for a, b in itertools.pairwise(p.route):
            key = (a.station_id, b.station_id, b.transport_in)
            if key in seen or a.station_id == b.station_id:
                continue
            seen.add(key)
            A, B = project.layout.station(a.station_id).center(), project.layout.station(b.station_id).center()
            d = abs(A.x - B.x) + abs(A.y - B.y)
            if vehicle_dist:
                d = vehicle_dist[b.transport_in]
            routes.append(Route(from_id=a.station_id, to_id=b.station_id, vehicle=b.transport_in,
                                points=[A, B], distance_m=d, mode="manhattan"))
    return RouteSet(layout_id=project.layout.id, mode="manhattan", routes=routes)


def make_project(stations: list[Workstation], products: list[Product], resources: list[ResourceConfig] | None = None,
                 **cfg) -> Project:
    layout = Layout(
        id="L-test", name="test", facility=Facility(width=100, height=20), workstations=stations,
        corridors=[Corridor(id="c", points=[Point(x=0, y=10), Point(x=100, y=10)])],
    )
    cfg.setdefault("resources", resources or [])
    return Project(id="P-test", name="test", layout=layout, products=products, sim_config=SimConfig(**cfg))


def ws(id_: str, x: float, proc: float, **kw) -> Workstation:
    return Workstation(id=id_, name=id_, x=x, y=2, width=4, length=3, processing_time_s=proc, **kw)


def product(route: list[str], rate: float, vehicle: VehicleType = OP, pid: str = "p", **kw) -> Product:
    steps = [RouteStep(station_id=s, transport_in=vehicle) for s in route]
    return Product(id=pid, name=pid, route=steps, arrival_rate_per_hour=rate, **kw)


def entity_events(result):
    by: dict[str, list] = {}
    for e in result.events:
        by.setdefault(e.entity_id, []).append(e)
    return by


# ------------------------------------------------------------------ olay sırası
ALLOWED_NEXT = {
    None: {ET.ENTITY_CREATED},
    ET.ENTITY_CREATED: {ET.QUEUE_ENTERED},
    ET.QUEUE_ENTERED: {ET.PROCESS_STARTED},
    ET.PROCESS_STARTED: {ET.PROCESS_FINISHED},
    ET.PROCESS_FINISHED: {ET.TRANSPORT_REQUESTED, ET.ENTITY_COMPLETED},
    ET.TRANSPORT_REQUESTED: {ET.TRANSPORT_STARTED},
    ET.TRANSPORT_STARTED: {ET.TRANSPORT_COMPLETED},
    ET.TRANSPORT_COMPLETED: {ET.QUEUE_ENTERED},
    ET.ENTITY_COMPLETED: set(),
}


def test_event_sequence_valid_and_all_types_present():
    proj = build_demo_project()
    res = run_simulation(proj, manhattan_routes(proj))
    assert {e.type for e in res.events} == set(EventType)
    for evs in entity_events(res).values():
        prev = None
        t_prev = -1.0
        for e in evs:
            assert e.type in ALLOWED_NEXT[prev], (e.entity_id, prev, e.type)
            assert e.t >= t_prev
            prev, t_prev = e.type, e.t
    # tamamlanan her varlık tüm rotayı sırayla gezmiş olmalı
    route = [s.station_id for s in proj.products[0].route]
    for evs in entity_events(res).values():
        if evs[-1].type == ET.ENTITY_COMPLETED:
            started = [e.station_id for e in evs if e.type == ET.PROCESS_STARTED]
            assert started == route


def test_capacity_never_exceeded_and_resource_ids_exclusive():
    proj = build_demo_project()
    proj.sim_config = proj.sim_config.model_copy(update={"arrival": "poisson", "seed": 3})
    res = run_simulation(proj, manhattan_routes(proj))
    cap = {w.id: w.capacity for w in proj.layout.workstations}
    n_res = {rc.vehicle: rc.count for rc in proj.sim_config.resources}
    in_proc = dict.fromkeys(cap, 0)
    busy_ids: set[str] = set()
    for e in res.events:
        if e.type == ET.PROCESS_STARTED:
            in_proc[e.station_id] += 1
            assert in_proc[e.station_id] <= cap[e.station_id]
        elif e.type == ET.PROCESS_FINISHED:
            in_proc[e.station_id] -= 1
        if e.type in (ET.PROCESS_STARTED, ET.TRANSPORT_STARTED) and e.resource_id:
            assert e.resource_id not in busy_ids, f"{e.resource_id} çift tahsis"
            busy_ids.add(e.resource_id)
            vehicle, idx = e.resource_id.rsplit("-", 1)
            assert int(idx) <= n_res[VehicleType(vehicle)]
        elif e.type in (ET.PROCESS_FINISHED, ET.TRANSPORT_COMPLETED) and e.resource_id:
            busy_ids.discard(e.resource_id)


# ------------------------------------------------------------------ el hesabı
def test_dd1_overloaded_queue_waits_by_hand():
    """D/D/1: varış her 50 s (ilk t=0), işlem 100 s. k. müşteri 50k'da gelir, 100k'da başlar => bekleme 50k."""
    proj = make_project([ws("A", 5, 100)], [product(["A"], rate=72)], duration_s=1050, arrival="deterministic")
    res = run_simulation(proj, manhattan_routes(proj))
    created = {e.entity_id: e.t for e in res.events if e.type == ET.ENTITY_CREATED}
    started = {e.entity_id: e.t for e in res.events if e.type == ET.PROCESS_STARTED}
    for k in range(10):
        eid = f"p-{k + 1:05d}"
        assert created[eid] == pytest.approx(50 * k)
        assert started[eid] - created[eid] == pytest.approx(50 * k)
    # k=0..9 tamamlanır (bitiş 100(k+1) <= 1000): cycle_k = 100 + 50k => ort 325 ; kuyruk ort 225
    assert res.kpis.completed == 10
    assert res.kpis.avg_cycle_time_s == pytest.approx(325.0)
    assert res.kpis.avg_queue_s == pytest.approx(225.0)
    assert res.kpis.avg_processing_s == pytest.approx(100.0)
    assert res.stations[0].utilization == pytest.approx(1.0, abs=0.01)
    assert res.bottleneck_station_id == "A"


def test_dd1_light_load_has_no_queue():
    proj = make_project([ws("A", 5, 60)], [product(["A"], rate=36)], duration_s=3600, arrival="deterministic")
    res = run_simulation(proj, manhattan_routes(proj))
    assert res.kpis.avg_queue_wait_s == 0 and res.kpis.max_queue_len == 0
    assert res.kpis.avg_cycle_time_s == pytest.approx(60.0)
    assert res.stations[0].utilization == pytest.approx(0.6, abs=0.02)


def test_md1_low_load_matches_pollaczek_khinchine():
    """M/D/1, rho=0.5, s=60: Wq = rho*s/(2(1-rho)) = 30 s. 30 replikasyonun %95 GA'sı 30'u içermeli."""
    proj = make_project([ws("A", 5, 60)], [product(["A"], rate=30)], duration_s=100_000, replications=30,
                        warmup_s=5_000, seed=11)
    res = run_simulation(proj, manhattan_routes(proj))
    ci = res.ci95["avg_queue_wait_s"]
    assert ci.n == 30 and ci.lo <= 30.0 <= ci.hi
    assert res.kpis.avg_queue_wait_s == pytest.approx(30.0, rel=0.1)
    assert res.stations[0].utilization == pytest.approx(0.5, rel=0.05)


def test_single_transport_timing_by_hand():
    """A(100 s) -> B(50 s), forklift 80 m @2 m/s, yükleme 7 + boşaltma 3 => 40 s seyir + 10 s yükleme/boşaltma."""
    rc = ResourceConfig(vehicle=FK, count=1, speed_mps=2.0, load_time_s=7, unload_time_s=3)
    proj = make_project([ws("A", 5, 100), ws("B", 40, 50)], [product(["A", "B"], rate=3.6, vehicle=FK)],
                        resources=[rc], duration_s=900, arrival="deterministic")
    rs = RouteSet(layout_id="L-test", routes=[Route(from_id="A", to_id="B", vehicle=FK, points=[], distance_m=80.0)])
    res = run_simulation(proj, rs)
    ev = {(e.type, e.entity_id): e for e in res.events}
    e1 = "p-00001"
    assert ev[(ET.TRANSPORT_STARTED, e1)].t == pytest.approx(100)
    assert ev[(ET.TRANSPORT_COMPLETED, e1)].t == pytest.approx(100 + 7 + 40 + 3)
    assert ev[(ET.ENTITY_COMPLETED, e1)].t == pytest.approx(100 + 50 + 50)
    k = res.kpis
    assert k.avg_transport_s == pytest.approx(40) and k.avg_loadunload_s == pytest.approx(10)
    assert k.avg_cycle_time_s == pytest.approx(200)
    assert res.resources[0].distance_m > 0


def test_unreachable_or_missing_route_raises():
    rc = ResourceConfig(vehicle=FK, count=1, speed_mps=2.0)
    proj = make_project([ws("A", 5, 10), ws("B", 40, 10)], [product(["A", "B"], rate=10, vehicle=FK)], resources=[rc])
    with pytest.raises(RouteUnavailableError):
        run_simulation(proj, RouteSet(layout_id="L-test", routes=[]))
    bad = RouteSet(layout_id="L-test", routes=[
        Route(from_id="A", to_id="B", vehicle=FK, points=[], distance_m=0.0, reachable=False)])
    with pytest.raises(RouteUnavailableError):
        run_simulation(proj, bad)
    with pytest.raises(SimulationError):
        run_simulation(proj, RouteSet(layout_id="başka", routes=[]))
    no_res = proj.model_copy(update={"sim_config": proj.sim_config.model_copy(update={"resources": []})})
    good = RouteSet(layout_id="L-test", routes=[Route(from_id="A", to_id="B", vehicle=FK, points=[], distance_m=10.0)])
    with pytest.raises(SimulationError):
        run_simulation(no_res, good)


# ------------------------------------------------------------------ Little yasası
def test_littles_law():
    st = [ws("A", 5, 80), ws("B", 20, 100)]
    rc = ResourceConfig(vehicle=OP, count=2, speed_mps=1.0, load_time_s=5, unload_time_s=5)
    proj = make_project(st, [product(["A", "B"], rate=24)], resources=[rc], duration_s=400_000, warmup_s=20_000, seed=5)
    res = run_simulation(proj, manhattan_routes(proj, {OP: 10.0}))
    lam = res.kpis.throughput_per_hour / 3600.0
    assert res.kpis.avg_wip == pytest.approx(lam * res.kpis.avg_cycle_time_s, rel=0.03)


# ------------------------------------------------------------------ tekrarlanabilirlik / CRN
def test_same_seed_same_result_different_seed_differs():
    proj = build_demo_project()
    rs = manhattan_routes(proj)
    a = run_simulation(proj, rs)
    b = run_simulation(proj, rs)
    assert a.model_dump() == b.model_dump()
    c = run_simulation(proj, rs, proj.sim_config.model_copy(update={"seed": 43}))
    assert c.kpis != a.kpis
    assert [e.t for e in c.events if e.type == ET.ENTITY_CREATED] != [e.t for e in a.events if e.type == ET.ENTITY_CREATED]


def test_crn_same_arrivals_across_scenarios():
    proj = build_demo_project()
    base = run_simulation(proj, manhattan_routes(proj))
    cfg2 = proj.sim_config.model_copy(update={"resources": [
        ResourceConfig(vehicle=OP, count=3, speed_mps=1.2, load_time_s=5, unload_time_s=5),
        ResourceConfig(vehicle=FK, count=2, speed_mps=2.5, load_time_s=15, unload_time_s=15)]})
    scen = run_simulation(proj, manhattan_routes(proj, {OP: 3.0, FK: 5.0}), cfg2)
    ta = [e.t for e in base.events if e.type == ET.ENTITY_CREATED]
    tb = [e.t for e in scen.events if e.type == ET.ENTITY_CREATED]
    assert len(ta) == len(tb) > 0
    assert ta == tb


def test_crn_process_times_identical_with_variability():
    st = [ws("A", 5, 60), ws("B", 20, 40)]
    rc = ResourceConfig(vehicle=OP, count=1, speed_mps=1.0)
    p1 = make_project(st, [product(["A", "B"], rate=6)], resources=[rc], duration_s=20_000, seed=9,
                      processing_variability="triangular(±%20)", arrival="poisson")
    r1 = run_simulation(p1, manhattan_routes(p1, {OP: 5.0}))
    r2 = run_simulation(p1, manhattan_routes(p1, {OP: 50.0}))
    assert r1.kpis.avg_processing_s != 100.0           # değişkenlik gerçekten uygulanıyor
    assert r1.kpis.completed == r2.kpis.completed
    assert r1.kpis.avg_processing_s == pytest.approx(r2.kpis.avg_processing_s)


# ------------------------------------------------------------------ kaynaklar
def test_operator_1_to_2_effect():
    # 2 paralel makine, her işlem 100 s ve operatör ister; varış 60 s'de bir => 1 operatör yetmez.
    mk = ws("M", 5, 100, capacity=2, requires_operator=True)
    out = {}
    for n in (1, 2):
        rc = ResourceConfig(vehicle=OP, count=n, speed_mps=1.0)
        proj = make_project([mk], [product(["M"], rate=60)], resources=[rc], duration_s=20_000, arrival="deterministic")
        out[n] = run_simulation(proj, manhattan_routes(proj))
    k1, k2 = out[1].kpis, out[2].kpis
    assert k2.throughput_per_hour > k1.throughput_per_hour * 1.3
    assert k2.avg_cycle_time_s < k1.avg_cycle_time_s / 5
    assert k1.avg_resource_wait_s + k1.avg_queue_s > k2.avg_resource_wait_s + k2.avg_queue_s
    assert out[1].resources[0].utilization == pytest.approx(1.0, abs=0.01)
    assert out[2].kpis.avg_resource_wait_s == pytest.approx(0.0, abs=1e-9)


def test_queue_capacity_blocks_upstream():
    # A hızlı (10 s), B yavaş (100 s, queue_capacity=1). Kuyruk B'de 1'i geçmemeli; A blokajla bekler.
    proj = make_project(
        [ws("A", 5, 10), ws("B", 20, 100, queue_capacity=1)], [product(["A", "B"], rate=360)],
        resources=[ResourceConfig(vehicle=OP, count=1, speed_mps=1.0)], duration_s=5000, arrival="deterministic")
    res = run_simulation(proj, manhattan_routes(proj, {OP: 1.0}))
    assert max(e.queue_len for e in res.events if e.type == ET.QUEUE_ENTERED and e.station_id == "B") <= 1
    assert res.stations[1].max_queue_len <= 1
    assert res.kpis.avg_queue_s > 100        # blokaj/kabul bekleme süresi kuyruk zamanına yazılır


def test_max_wip_rejects_arrivals():
    proj = make_project([ws("A", 5, 100)], [product(["A"], rate=360, max_wip=3)], duration_s=2000,
                        arrival="deterministic")
    res = run_simulation(proj, manhattan_routes(proj))
    assert any("max_wip" in n for n in res.notes)
    assert res.kpis.avg_wip <= 3.0


# ------------------------------------------------------------------ zaman ayrışımı
def _decomp_sum(k):
    return (k.avg_processing_s + k.avg_setup_s + k.avg_transport_s + k.avg_loadunload_s
            + k.avg_resource_wait_s + k.avg_queue_s)


def test_time_decomposition_sums_to_cycle_time():
    proj = build_demo_project()
    proj.sim_config = proj.sim_config.model_copy(update={"arrival": "poisson", "warmup_s": 1800})
    res = run_simulation(proj, manhattan_routes(proj))
    assert res.kpis.completed > 0
    assert _decomp_sum(res.kpis) == pytest.approx(res.kpis.avg_cycle_time_s, rel=1e-9)
    # kuyruk kapasiteli + üçgen değişkenlik senaryosu
    proj2 = make_project(
        [ws("A", 5, 30), ws("B", 20, 90, queue_capacity=2, requires_operator=True, setup_time_s=5)],
        [product(["A", "B"], rate=50)],
        resources=[ResourceConfig(vehicle=OP, count=1, speed_mps=1.0, load_time_s=2, unload_time_s=2)],
        duration_s=30_000, processing_variability="triangular(±%20)")
    r2 = run_simulation(proj2, manhattan_routes(proj2, {OP: 12.0}))
    assert r2.kpis.completed > 0
    assert _decomp_sum(r2.kpis) == pytest.approx(r2.kpis.avg_cycle_time_s, rel=1e-9)


# ------------------------------------------------------------------ replikasyon / warmup / log sınırı
def test_replications_ci_and_seed_plus_i():
    proj = build_demo_project()
    cfg = proj.sim_config.model_copy(update={"replications": 4, "duration_s": 4 * 3600, "arrival": "poisson"})
    rs = manhattan_routes(proj)
    res = run_simulation(proj, rs, cfg)
    assert len(res.replication_kpis) == 4
    single = run_simulation(proj, rs, cfg.model_copy(update={"replications": 1, "seed": cfg.seed + 2}))
    assert res.replication_kpis[2] == single.kpis
    ci = res.ci95["throughput_per_hour"]
    assert ci.n == 4 and ci.lo <= ci.mean <= ci.hi
    assert ci.mean == pytest.approx(res.kpis.throughput_per_hour)
    assert ci.lo < ci.hi


def test_warmup_window_statistics():
    proj = make_project([ws("A", 5, 60)], [product(["A"], rate=36)], duration_s=7200, warmup_s=3600,
                        arrival="deterministic")
    res = run_simulation(proj, manhattan_routes(proj))
    assert res.kpis.completed == pytest.approx(36, abs=1)
    assert res.kpis.throughput_per_hour == pytest.approx(36, abs=1)
    assert res.stations[0].utilization == pytest.approx(0.6, abs=0.02)
    assert any("Isınma" in n for n in res.notes)


def test_event_log_truncation_note(monkeypatch):
    monkeypatch.setattr(engine, "MAX_EVENTS", 50)
    proj = build_demo_project()
    res = run_simulation(proj, manhattan_routes(proj))
    assert len(res.events) == 50
    assert any("kesildi" in n for n in res.notes)
    assert res.kpis.completed > 0


# ------------------------------------------------------------------ demo proje
def test_demo_project_runs_8h_and_is_reasonable():
    proj = build_demo_project()
    res = run_simulation(proj, manhattan_routes(proj))
    k = res.kpis
    assert res.config.duration_s == 8 * 3600
    assert max(e.t for e in res.events) <= 8 * 3600
    assert 0 < k.completed <= 99 and k.throughput_per_hour > 0
    route_ids = {x.station_id for x in proj.products[0].route}
    min_proc = sum(s.processing_time_s for s in proj.layout.workstations if s.id in route_ids)
    assert k.avg_cycle_time_s >= min_proc
    for s in res.stations:
        assert 0.0 <= s.utilization <= 1.0 + 1e-9
    for r in res.resources:
        assert 0.0 <= r.utilization <= 1.0 + 1e-9
    assert res.bottleneck_station_id in {w.id for w in proj.layout.workstations}
    assert k.total_travel_distance_m > 0 and "boş seyir" in res.not_modelled
    print("\nDEMO KPI:", k.model_dump())
    print("DEMO ST:", [(s.station_id, round(s.utilization, 3), round(s.avg_queue_wait_s, 1), s.max_queue_len)
                       for s in res.stations])
    print("DEMO RES:", [(r.vehicle.value, round(r.utilization, 3), round(r.distance_m, 1)) for r in res.resources])
    print("DEMO BOTTLENECK:", res.bottleneck_station_id)
