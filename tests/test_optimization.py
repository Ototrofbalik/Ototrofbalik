"""Optimizasyon testleri. Demo üzerinde GERÇEK sonuçlar (sabit kodlu beklenen iyileşme yok; yalnız tutarlılık,
geçerlilik, determinism ve dürüst raporlama doğrulanır). `pytest -s` demo sonuçlarını yazdırır."""
from __future__ import annotations

import pytest

from backend.demo.demo_factory import build_demo_project
from backend.layout.validation import validate_layout
from backend.optimization.engine import optimize_layout
from backend.optimization.evaluator import Evaluator
from backend.optimization.placement import PlacementSpace
from backend.routing.engine import compute_flow_metrics, compute_routes, derive_flow_edges
from backend.schemas import (
    Corridor,
    Facility,
    Layout,
    OptimizationConfig,
    Point,
    Product,
    Project,
    ResourceConfig,
    Rotation,
    RouteStep,
    SimConfig,
    VehicleType,
    Workstation,
)

FAST = dict(iterations=60, simulate_alternatives=False)


def _sig(layout: Layout) -> tuple:
    return tuple(sorted((s.id, round(s.x, 6), round(s.y, 6), s.rotation.value) for s in layout.workstations))


@pytest.fixture(scope="module")
def demo():
    return build_demo_project()


@pytest.fixture(scope="module")
def demo_result(demo):
    return optimize_layout(demo, OptimizationConfig(target_distance_reduction_pct=30.0))


# ------------------------------------------------------------------ demo (varsayılan config, simülasyon dahil)
def test_demo_real_results(demo, demo_result):
    r = demo_result
    edges = derive_flow_edges(demo)
    base = compute_flow_metrics(demo.layout, edges, compute_routes(demo.layout, edges), demo)
    assert r.baseline_flow.total_distance_m_per_hour == pytest.approx(base.total_distance_m_per_hour)
    assert r.baseline_flow.total_distance_m_per_hour == pytest.approx(1540.0)
    assert len(r.alternatives) >= r.config.n_alternatives
    best = next(a for a in r.alternatives if a.id == r.best_alternative_id)
    print("\nDEMO baseline D_total:", r.baseline_flow.total_distance_m_per_hour)
    for a in r.alternatives:
        print(f"  {a.id} {a.algorithm:20s} D={a.flow.total_distance_m_per_hour:.1f} "
              f"red=%{a.distance_reduction_pct:.1f} obj={a.objective:.4f} moved={a.moved_station_ids} "
              f"thr={a.sim.kpis.throughput_per_hour:.3f}")
    print("  best:", r.best_alternative_id, "target_met:", r.target_met)
    print("  rationale:", r.rationale)
    print("  notes:", r.infeasibility_notes)
    assert best.flow.total_distance_m_per_hour <= r.baseline_flow.total_distance_m_per_hour
    assert f"{best.flow.total_distance_m_per_hour:.1f}" in r.rationale
    assert f"{r.baseline_flow.total_distance_m_per_hour:.1f}" in r.rationale


def test_all_alternatives_valid_distinct_and_recomputed(demo, demo_result):
    edges = derive_flow_edges(demo)
    d0 = demo_result.baseline_flow.total_distance_m_per_hour
    sigs = {_sig(demo.layout)}
    for a in demo_result.alternatives:
        rep = validate_layout(a.layout)
        assert rep.valid and a.validation.valid, rep.issues
        assert not rep.issues
        flow = compute_flow_metrics(a.layout, edges, compute_routes(a.layout, edges), demo)
        assert flow.total_distance_m_per_hour == pytest.approx(a.flow.total_distance_m_per_hour)
        assert a.distance_reduction_pct == pytest.approx((d0 - flow.total_distance_m_per_hour) / d0 * 100)
        assert a.flow.mode == "network"
        # moved_station_ids gerçekten değişenler
        moved = sorted(s.id for s in a.layout.workstations
                       if (s.x, s.y, s.rotation) != (lambda b: (b.x, b.y, b.rotation))(demo.layout.station(s.id)))
        assert moved == sorted(a.moved_station_ids)
        assert a.layout.parent_id == demo.layout.id
        sigs.add(_sig(a.layout))
    assert len(sigs) == len(demo_result.alternatives) + 1   # birbirinden ve baseline'dan farklı


def test_best_not_worse_than_baseline(demo_result):
    r = demo_result
    best = next(a for a in r.alternatives if a.id == r.best_alternative_id)
    base_obj = r.config.weights.distance + r.config.weights.walking   # normalize: bileşen>0 olanların ağırlık toplamı
    assert best.objective < base_obj
    assert best.flow.total_distance_m_per_hour <= r.baseline_flow.total_distance_m_per_hour
    eligible = [a for a in r.alternatives if a.flow.total_distance_m_per_hour <= r.baseline_flow.total_distance_m_per_hour]
    assert best.objective == min(a.objective for a in eligible if a.distance_reduction_pct >= 30.0)


def test_simulation_crn_same_seed(demo, demo_result):
    r = demo_result
    assert r.baseline_sim is not None
    for a in r.alternatives:
        assert a.sim is not None
        assert a.sim.seed == r.baseline_sim.seed == demo.sim_config.seed
        assert a.sim.config == r.baseline_sim.config
        assert a.sim.layout_id == a.layout.id


def test_target_met_flag(demo_result):
    assert demo_result.target_met is True
    best = next(a for a in demo_result.alternatives if a.id == demo_result.best_alternative_id)
    assert best.distance_reduction_pct >= 30.0


# ------------------------------------------------------------------ dürüstlük / kısıtlar
def test_impossible_target_reports_honestly(demo):
    r = optimize_layout(demo, OptimizationConfig(target_distance_reduction_pct=99.0, **FAST))
    assert r.target_met is False
    assert "KARŞILANAMADI" in r.rationale
    best_red = max(a.distance_reduction_pct for a in r.alternatives)
    assert f"%{best_red:.1f}" in r.rationale


def test_no_target_gives_none(demo):
    r = optimize_layout(demo, OptimizationConfig(**FAST))
    assert r.target_met is None


def test_fixed_stations_never_move(demo):
    fixed = {"drill", "asm", "ship"}
    lay = demo.layout.model_copy(update={"workstations": [
        s.model_copy(update={"fixed": s.id in fixed}) for s in demo.layout.workstations]})
    proj = demo.model_copy(update={"layout": lay})
    r = optimize_layout(proj, OptimizationConfig(**FAST))
    assert r.alternatives
    for a in r.alternatives:
        assert not fixed & set(a.moved_station_ids)
        for sid in fixed:
            s0, s1 = lay.station(sid), a.layout.station(sid)
            assert (s0.x, s0.y, s0.rotation) == (s1.x, s1.y, s1.rotation)
        assert validate_layout(a.layout).valid


def test_all_fixed_no_alternatives(demo):
    lay = demo.layout.model_copy(update={"workstations": [
        s.model_copy(update={"fixed": True}) for s in demo.layout.workstations]})
    r = optimize_layout(demo.model_copy(update={"layout": lay}), OptimizationConfig(**FAST))
    assert r.alternatives == [] and r.best_alternative_id is None
    assert any("Taşınabilir" in n for n in r.infeasibility_notes)
    assert "bulunamadı" in r.rationale


def test_determinism_same_seed(demo):
    cfg = OptimizationConfig(seed=11, **FAST)
    a = optimize_layout(demo, cfg)
    b = optimize_layout(demo, cfg)
    assert a.model_dump() == b.model_dump()


@pytest.mark.parametrize("algo", ["swap_search", "local_search", "simulated_annealing", "ortools_assignment"])
def test_each_algorithm_as_primary(demo, algo):
    r = optimize_layout(demo, OptimizationConfig(algorithm=algo, n_alternatives=1, **FAST))
    assert r.alternatives
    assert r.alternatives[0].algorithm == algo   # birincil algoritmanın sonucu ilk sırada
    for a in r.alternatives:
        assert validate_layout(a.layout).valid
        assert a.distance_reduction_pct > 0 or a.objective < r.config.weights.distance + r.config.weights.walking


def test_unknown_algorithm_rejected(demo):
    with pytest.raises(ValueError):
        optimize_layout(demo, OptimizationConfig(algorithm="genetic", **FAST))


def test_adjacency_pairs_affect_objective(demo):
    cfg = OptimizationConfig(adjacency_pairs=[("raw", "ship")], weights={"distance": 0.2, "walking": 0.0,
                                                                         "adjacency": 2.0, "area": 0.0}, **FAST)
    r = optimize_layout(demo, cfg)
    best = next(a for a in r.alternatives if a.id == r.best_alternative_id) if r.best_alternative_id else None
    c0, c1 = demo.layout.station("raw").center(), demo.layout.station("ship").center()
    d_base = ((c0.x - c1.x) ** 2 + (c0.y - c1.y) ** 2) ** 0.5
    assert r.alternatives
    a = min(r.alternatives, key=lambda a: a.objective)
    ra, sa = a.layout.station("raw").center(), a.layout.station("ship").center()
    assert ((ra.x - sa.x) ** 2 + (ra.y - sa.y) ** 2) ** 0.5 < d_base
    if best is not None:
        assert best.flow.total_distance_m_per_hour <= r.baseline_flow.total_distance_m_per_hour


# ------------------------------------------------------------------ erişilemeyen akış = uygulanamaz aday
def _two_island_project() -> Project:
    def ws(i, x):
        return Workstation(id=i, name=i, x=x, y=1.0, width=3, length=2,
                           input_offset=Point(x=-1, y=0), output_offset=Point(x=1, y=0))
    lay = Layout(id="islands", name="islands", facility=Facility(width=40, height=10),
                 workstations=[ws("a", 2.0), ws("b", 8.0)],
                 corridors=[Corridor(id="c1", points=[Point(x=1, y=5), Point(x=15, y=5)], width=2.0),
                            Corridor(id="c2", points=[Point(x=25, y=5), Point(x=39, y=5)], width=2.0)])
    prod = Product(id="p", name="p", route=[RouteStep(station_id="a"), RouteStep(station_id="b")],
                   arrival_rate_per_hour=10)
    sim = SimConfig(resources=[ResourceConfig(vehicle=VehicleType.OPERATOR, count=1, speed_mps=1.0)])
    return Project(id="islands", name="islands", layout=lay, products=[prod], sim_config=sim)


def test_unreachable_candidate_is_infeasible():
    proj = _two_island_project()
    edges = derive_flow_edges(proj)
    sp = PlacementSpace(proj.layout, ["a", "b"])
    ev = Evaluator(proj, edges, sp, [])
    ok = ev.exact({"a": (2.0, 1.0, Rotation.R0), "b": (8.0, 1.0, Rotation.R0)})
    assert ok.feasible
    bad = ev.exact({"a": (2.0, 1.0, Rotation.R0), "b": (30.0, 1.0, Rotation.R0)})
    assert not bad.feasible and "UNREACHABLE_FLOW" in bad.reason
    r = optimize_layout(proj, OptimizationConfig(**FAST))
    for a in r.alternatives:   # hiçbir alternatif adaya bölünmüş akış içermez
        assert validate_layout(a.layout).valid
        assert all(c.distance_m > 0 for c in a.flow.cells)


def test_surrogate_matches_exact_on_baseline(demo):
    edges = derive_flow_edges(demo)
    ids = [s.id for s in demo.layout.workstations]
    sp = PlacementSpace(demo.layout, ids)
    ev = Evaluator(demo, edges, sp, [])
    st = {s.id: (s.x, s.y, s.rotation) for s in demo.layout.workstations}
    assert ev.surrogate(st).distance == pytest.approx(ev.exact(st).comps.distance)
