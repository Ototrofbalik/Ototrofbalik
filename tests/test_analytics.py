"""Analytics testleri: darboğaz sıralaması ve senaryo karşılaştırma."""
from __future__ import annotations

import pytest

from backend.analytics.bottleneck import analyze_bottlenecks, compare_results
from backend.demo.demo_factory import build_demo_project
from backend.schemas import FlowMetrics, ResourceConfig, VehicleType
from backend.simulation.engine import run_simulation
from tests.test_simulation import manhattan_routes


@pytest.fixture(scope="module")
def demo():
    proj = build_demo_project()
    return proj, manhattan_routes(proj)


def test_bottleneck_ranking_uses_computed_numbers(demo):
    proj, rs = demo
    res = run_simulation(proj, rs)
    a = analyze_bottlenecks(res)
    by_id = {s.station_id: s for s in res.stations}
    scores = [r["score"] for r in a["ranking"]]
    assert scores == sorted(scores, reverse=True)
    assert a["bottleneck_station_id"] == a["ranking"][0]["station_id"] == res.bottleneck_station_id
    for r in a["ranking"]:
        assert r["utilization"] == by_id[r["station_id"]].utilization
        assert r["max_queue_len"] == by_id[r["station_id"]].max_queue_len
    assert sum(a["time_breakdown_share"].values()) == pytest.approx(1.0)
    assert {c["vehicle"] for c in a["resource_constraints"]} == {"operator", "forklift"}
    assert a["findings"]


def test_bottleneck_none_when_nothing_ran(demo):
    proj, rs = demo
    res = run_simulation(proj, rs, proj.sim_config.model_copy(update={"duration_s": 1.0, "arrival": "poisson", "seed": 1}))
    assert res.bottleneck_station_id is None or res.bottleneck_station_id == "raw"


def _scenario(proj, rs, ops: int):
    cfg = proj.sim_config.model_copy(update={"resources": [
        ResourceConfig(vehicle=VehicleType.OPERATOR, count=ops, speed_mps=1.2, load_time_s=5, unload_time_s=5),
        ResourceConfig(vehicle=VehicleType.FORKLIFT, count=1, speed_mps=2.5, load_time_s=15, unload_time_s=15)]})
    return run_simulation(proj, rs, cfg)


def test_compare_results_directions_and_savings(demo):
    proj, rs = demo
    base, scen = _scenario(proj, rs, 2), _scenario(proj, rs, 3)
    cmp = compare_results(base, scen, project=proj)
    d = {x.name: x for x in cmp.deltas}
    assert cmp.same_seed and cmp.seed == base.config.seed
    assert d["throughput_per_hour"].delta_abs == pytest.approx(
        scen.kpis.throughput_per_hour - base.kpis.throughput_per_hour)
    assert d["avg_cycle_time_s"].delta_pct == pytest.approx(
        (scen.kpis.avg_cycle_time_s - base.kpis.avg_cycle_time_s) / base.kpis.avg_cycle_time_s * 100)
    assert d["avg_processing_s"].better is None
    assert cmp.savings is not None and "travel_cost_per_hour" in cmp.savings
    # operatör sayısı 2->3: operatör darboğazı azalır => verim artar, çevrim süresi düşer
    assert d["throughput_per_hour"].better is True and d["avg_cycle_time_s"].better is True


def test_savings_only_with_cost(demo):
    proj, rs = demo
    base = _scenario(proj, rs, 2)
    assert compare_results(base, base).savings is None
    no_cost = proj.model_copy(update={"cost": None})
    assert compare_results(base, base, project=no_cost).savings is None
    cmp = compare_results(base, base, project=proj)
    assert cmp.savings["travel_cost_per_hour"] == pytest.approx(0.0)


def test_compare_flags_different_seed_and_flow(demo):
    proj, rs = demo
    a = run_simulation(proj, rs)
    b = run_simulation(proj, rs, proj.sim_config.model_copy(update={"seed": 1}))
    f1 = FlowMetrics(layout_id="x", mode="network", total_distance_m_per_hour=100, distance_per_unit_m=10,
                     operator_walking_m_per_hour=40, forklift_m_per_hour=60, agv_m_per_hour=0,
                     transport_time_s_per_hour=50, cells=[])
    f2 = f1.model_copy(update={"total_distance_m_per_hour": 80, "transport_time_s_per_hour": 40})
    cmp = compare_results(a, b, f1, f2)
    assert not cmp.same_seed and any("CRN" in n for n in cmp.notes)
    fd = {x.name: x for x in cmp.deltas}
    assert fd["flow.total_distance_m_per_hour"].delta_abs == -20
    assert fd["flow.total_distance_m_per_hour"].better is True
