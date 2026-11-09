"""[QA] Kontrol 3 — optimizasyon bağımsız doğrulaması.

Çakışma/sınır/engel kontrolü burada ELLE (validate_layout'tan bağımsız) yazılmıştır; ayrıca validate_layout yeniden çalıştırılır.
"""
from __future__ import annotations

import pytest

from backend.layout.validation import validate_layout
from backend.optimization.engine import optimize_layout
from backend.routing.engine import compute_flow_metrics, compute_routes, derive_flow_edges
from backend.schemas import OptimizationConfig


def _rect(ws):
    f = ws.footprint()
    return f.x, f.y, f.x + f.w, f.y + f.h


def _overlap(a, b):
    return a[0] < b[2] - 1e-9 and b[0] < a[2] - 1e-9 and a[1] < b[3] - 1e-9 and b[1] < a[3] - 1e-9


def manual_geometry_errors(layout) -> list[str]:
    errs = []
    W, H = layout.facility.width, layout.facility.height
    rects = {w.id: _rect(w) for w in layout.workstations}
    for sid, r in rects.items():
        if r[0] < -1e-9 or r[1] < -1e-9 or r[2] > W + 1e-9 or r[3] > H + 1e-9:
            errs.append(f"{sid} dışarıda")
        for o in layout.obstacles:
            ob = (o.rect.x, o.rect.y, o.rect.x + o.rect.w, o.rect.y + o.rect.h)
            if _overlap(r, ob):
                errs.append(f"{sid} engelle çakışıyor ({o.id})")
    ids = list(rects)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if _overlap(rects[ids[i]], rects[ids[j]]):
                errs.append(f"{ids[i]}-{ids[j]} çakışıyor")
    return errs


def test_at_least_three_alternatives_all_valid(analysis, demo_project):
    opt = analysis.optimization
    assert len(opt.alternatives) >= 3
    ids = [a.id for a in opt.alternatives]
    assert len(set(ids)) == len(ids)
    for alt in opt.alternatives:
        assert validate_layout(alt.layout).valid, alt.id                 # yeniden doğrula
        assert alt.validation.valid
        assert manual_geometry_errors(alt.layout) == [], alt.id          # bağımsız geometri
        assert {w.id for w in alt.layout.workstations} == {w.id for w in demo_project.layout.workstations}
        # boyut/süre/kapasite değişmemeli (yalnız konum/dönüş)
        for w in alt.layout.workstations:
            o = demo_project.layout.station(w.id)
            assert (w.width, w.length, w.processing_time_s, w.setup_time_s, w.capacity, w.requires_operator) == \
                   (o.width, o.length, o.processing_time_s, o.setup_time_s, o.capacity, o.requires_operator)
        assert alt.layout.obstacles == demo_project.layout.obstacles
        assert alt.layout.corridors == demo_project.layout.corridors
        assert alt.layout.id != demo_project.layout.id


def test_alternative_distance_claims_are_recomputable(analysis, demo_project):
    base_d = analysis.flow.total_distance_m_per_hour
    edges = derive_flow_edges(demo_project)
    for alt in analysis.optimization.alternatives:
        proj = demo_project.model_copy(update={"layout": alt.layout}, deep=True)
        routes = compute_routes(alt.layout, edges)
        assert not routes.unreachable
        flow = compute_flow_metrics(alt.layout, edges, routes, proj)
        assert flow.total_distance_m_per_hour == pytest.approx(alt.flow.total_distance_m_per_hour)
        assert alt.distance_reduction_pct == pytest.approx((base_d - flow.total_distance_m_per_hour) / base_d * 100)
        moved = sorted(w.id for w in alt.layout.workstations
                       if (w.x, w.y, w.rotation) != (demo_project.layout.station(w.id).x,
                                                      demo_project.layout.station(w.id).y,
                                                      demo_project.layout.station(w.id).rotation))
        assert moved == sorted(alt.moved_station_ids)


def test_best_alternative_is_not_worse_than_baseline_and_is_the_pick(analysis):
    opt = analysis.optimization
    best = next(a for a in opt.alternatives if a.id == opt.best_alternative_id)
    assert best.flow.total_distance_m_per_hour < opt.baseline_flow.total_distance_m_per_hour
    assert opt.baseline_flow.total_distance_m_per_hour == pytest.approx(1540.0)
    assert opt.baseline_layout_id == "demo-layout-v1"
    assert all(a.sim is not None for a in opt.alternatives) and opt.baseline_sim is not None


def test_input_project_not_mutated(demo_project, analysis):
    from backend.demo.demo_factory import build_demo_project

    assert demo_project.model_dump() == build_demo_project().model_dump()


def test_fixed_stations_never_move():
    from backend.demo.demo_factory import build_demo_project

    p = build_demo_project()
    p.layout.workstations = [w.model_copy(update={"fixed": w.id in ("raw", "ship", "drill")})
                             for w in p.layout.workstations]
    res = optimize_layout(p, OptimizationConfig(iterations=150, simulate_alternatives=False, n_alternatives=3))
    assert res.alternatives
    for alt in res.alternatives:
        for sid in ("raw", "ship", "drill"):
            a, b = alt.layout.station(sid), p.layout.station(sid)
            assert (a.x, a.y, a.rotation) == (b.x, b.y, b.rotation), (alt.id, sid)
        assert "raw" not in alt.moved_station_ids and "ship" not in alt.moved_station_ids
        assert validate_layout(alt.layout).valid and manual_geometry_errors(alt.layout) == []


def test_unreachable_target_is_honest_not_met(demo_project):
    """'%99 azaltma' fiziksel olarak imkânsız: target_met=False, açıklayıcı not; uydurma başarı yok."""
    res = optimize_layout(demo_project, OptimizationConfig(
        iterations=200, simulate_alternatives=False, target_distance_reduction_pct=99.0))
    assert res.target_met is False
    best = [a for a in res.alternatives if a.id == res.best_alternative_id]
    if best:
        assert best[0].distance_reduction_pct < 99.0
    assert all(a.distance_reduction_pct < 99.0 for a in res.alternatives)
    assert res.infeasibility_notes or "sağlanamadı" in res.rationale.lower() or "hedef" in res.rationale.lower()


def test_modest_target_is_met_when_achievable(demo_project):
    res = optimize_layout(demo_project, OptimizationConfig(
        iterations=200, simulate_alternatives=False, target_distance_reduction_pct=20.0))
    assert res.target_met is True
    best = next(a for a in res.alternatives if a.id == res.best_alternative_id)
    assert best.distance_reduction_pct >= 20.0


@pytest.mark.parametrize("algo", ["swap_search", "local_search", "simulated_annealing", "ortools_assignment"])
def test_every_algorithm_returns_only_valid_alternatives(demo_project, algo):
    res = optimize_layout(demo_project, OptimizationConfig(algorithm=algo, iterations=60, seed=3,
                                                           simulate_alternatives=False))
    for alt in res.alternatives:
        assert validate_layout(alt.layout).valid, (algo, alt.id)
        assert manual_geometry_errors(alt.layout) == []
        assert alt.sim is None   # simulate_alternatives=False -> sahte sim yok


def test_optimizer_deterministic_for_same_seed(demo_project):
    cfg = OptimizationConfig(iterations=80, seed=11, simulate_alternatives=False)
    a = optimize_layout(demo_project, cfg)
    b = optimize_layout(demo_project, cfg)
    assert [x.flow.total_distance_m_per_hour for x in a.alternatives] == [x.flow.total_distance_m_per_hour for x in b.alternatives]
    assert a.best_alternative_id == b.best_alternative_id
