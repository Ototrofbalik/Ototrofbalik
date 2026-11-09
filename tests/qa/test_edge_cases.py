"""[QA] Kontrol 9 — kenar durumlar: erişilemeyen rota, boş layout, tek istasyon, 0 kaynaklı sim, büyük olay logu."""
from __future__ import annotations

import pytest

from backend.layout.editing import move_workstation
from backend.layout.validation import validate_layout
from backend.pipeline import analyze_facility
from backend.routing.engine import (
    UnreachableFlowError,
    compute_flow_metrics,
    compute_routes,
    derive_flow_edges,
)
from backend.schemas import (
    EventType,
    Facility,
    Layout,
    Product,
    Project,
    ResourceConfig,
    RouteStep,
    VehicleType,
)
from backend.simulation.engine import MAX_EVENTS, RouteUnavailableError, SimulationError, run_simulation


def test_unreachable_station_is_flagged_everywhere_no_fake_numbers(demo_project):
    lay = move_workstation(demo_project.layout, "raw", 26, 17)       # koridora >6 m
    rep = validate_layout(lay)
    assert not rep.valid and any(i.code == "NO_CORRIDOR_ACCESS" and "raw" in i.entity_ids for i in rep.issues)
    proj = demo_project.model_copy(update={"layout": lay}, deep=True)
    edges = derive_flow_edges(proj)
    routes = compute_routes(lay, edges)
    assert ("raw", "cut", "forklift") in routes.unreachable
    with pytest.raises(UnreachableFlowError):
        compute_flow_metrics(lay, edges, routes, proj)                # sessizce 0 m yazmaz
    with pytest.raises(RouteUnavailableError):
        run_simulation(proj, routes)
    a = analyze_facility(proj, optimize=True)                         # pipeline durur, sahte sonuç üretmez
    assert [s.step for s in a.steps] == ["validate_layout"] and not a.steps[0].ok
    assert a.flow is None and a.baseline_sim is None and a.optimization is None


def test_offline_analysis_text_for_invalid_layout_has_no_numbers(db, tmp_path):
    from backend import services
    from backend.agent.orchestrator import FactoryFlowAgent
    from backend.agent.tool_executor import ToolExecutor
    from backend.agent.tool_registry import ToolContext

    lay = move_workstation(services.get_layout("demo-project"), "raw", 26, 17)
    services.save_layout_version("demo-project", lay.model_copy(update={"id": "bad-v2"}))
    reply = FactoryFlowAgent(None, ToolExecutor(ToolContext(artifact_dir=tmp_path))).handle(
        "Bu tesisi analiz et", "demo-project")
    assert "GEÇERSİZ" in reply.text and "çalıştırılmadı" in reply.text
    assert "Malzeme akışı" not in reply.text and "Darboğaz istasyonu" not in reply.text
    assert "throughput" not in reply.text


def test_empty_layout_without_products_gives_explicit_error(demo_project):
    empty = Layout(id="e", name="e", facility=Facility(width=30, height=20), workstations=[], corridors=[])
    p = Project(id="e", name="e", layout=empty, products=[], sim_config=demo_project.sim_config)
    assert validate_layout(empty).valid          # boş layout geometrik olarak geçerli
    with pytest.raises(SimulationError, match="ürün yok"):
        analyze_facility(p, optimize=True)


@pytest.mark.xfail(strict=True, raises=pytest.fail.Exception, reason="F-05: ürün rotası olmayan istasyona başvuruyor -> pipeline ham KeyError ('raw'); açık hata/UNKNOWN_STATION yok")
def test_products_referencing_missing_stations_get_clear_error(demo_project):
    empty = Layout(id="e", name="e", facility=Facility(width=30, height=20), workstations=[], corridors=[])
    p = Project(id="e", name="e", layout=empty, products=demo_project.products, sim_config=demo_project.sim_config)
    try:
        analyze_facility(p, optimize=True)
    except (SimulationError, ValueError):
        return                                       # açık, anlaşılır hata kabul
    except KeyError as exc:
        pytest.fail(f"ham KeyError sızdı: {exc!r}")


def test_single_station_runs_and_optimizer_is_honest(demo_project):
    one = Layout(id="o", name="o", facility=Facility(width=30, height=20),
                 workstations=[demo_project.layout.station("raw")], corridors=demo_project.layout.corridors)
    prod = Product(id="x", name="x", arrival_rate_per_hour=5, route=[RouteStep(station_id="raw")])
    p = Project(id="o", name="o", layout=one, products=[prod], sim_config=demo_project.sim_config)
    a = analyze_facility(p, optimize=True)
    assert all(s.ok for s in a.steps)
    assert a.flow.total_distance_m_per_hour == 0.0
    assert a.baseline_sim.kpis.total_travel_distance_m == 0.0 and a.baseline_sim.kpis.completed > 0
    assert a.optimization.alternatives == [] and a.optimization.best_alternative_id is None
    assert a.optimization.rationale and a.comparison is None          # iyileştirme iddiası yok


def test_zero_resources_explicit_error_when_transport_needed(demo_project):
    routes = compute_routes(demo_project.layout, derive_flow_edges(demo_project))
    cfg0 = demo_project.sim_config.model_copy(update={"resources": [
        ResourceConfig(vehicle=VehicleType.OPERATOR, count=0, speed_mps=1.2),
        ResourceConfig(vehicle=VehicleType.FORKLIFT, count=0, speed_mps=2.5)]})
    with pytest.raises(SimulationError, match="forklift"):
        run_simulation(demo_project, routes, cfg0)
    with pytest.raises(SimulationError):
        run_simulation(demo_project, routes, demo_project.sim_config.model_copy(update={"resources": []}))


def test_zero_resources_ok_when_no_transport_or_operator_needed(demo_project):
    st = demo_project.layout.station("raw")
    one = Layout(id="o", name="o", facility=demo_project.layout.facility, workstations=[st],
                 corridors=demo_project.layout.corridors)
    prod = Product(id="x", name="x", arrival_rate_per_hour=5, route=[RouteStep(station_id="raw")])
    p = Project(id="o", name="o", layout=one, products=[prod], sim_config=demo_project.sim_config)
    cfg0 = p.sim_config.model_copy(update={"resources": [
        ResourceConfig(vehicle=VehicleType.OPERATOR, count=0, speed_mps=1.2)]})
    r = run_simulation(p, compute_routes(one, derive_flow_edges(p)), cfg0)
    assert r.kpis.completed > 0 and r.kpis.total_travel_distance_m == 0


def test_requires_operator_station_without_operator_is_error(demo_project):
    routes = compute_routes(demo_project.layout, derive_flow_edges(demo_project))
    cfg = demo_project.sim_config.model_copy(update={"resources": [
        r for r in demo_project.sim_config.resources if r.vehicle != VehicleType.OPERATOR]})
    with pytest.raises(SimulationError):
        run_simulation(demo_project, routes, cfg)


def test_huge_event_log_is_truncated_with_note_but_kpis_use_all_events(demo_project):
    routes = compute_routes(demo_project.layout, derive_flow_edges(demo_project))
    cfg = demo_project.sim_config.model_copy(update={"duration_s": 400 * 3600})
    r = run_simulation(demo_project, routes, cfg)
    assert len(r.events) == MAX_EVENTS == 100_000
    assert any("kesildi" in n and "KPI" in n for n in r.notes)
    in_log = sum(1 for e in r.events if e.type == EventType.ENTITY_COMPLETED)
    assert r.kpis.completed > in_log                    # KPI log'dan değil tüm koşudan hesaplanmış
    assert r.kpis.completed == pytest.approx(r.kpis.throughput_per_hour * 400, abs=1)
    # küçük koşuda kesme notu YOK (yanlış alarm yok)
    small = run_simulation(demo_project, routes)
    assert not any("kesildi" in n for n in small.notes)


def test_zero_duration_and_warmup_ge_duration_rejected(demo_project):
    routes = compute_routes(demo_project.layout, derive_flow_edges(demo_project))
    for upd in ({"duration_s": 0}, {"duration_s": -1}, {"replications": 0}):
        with pytest.raises(Exception):  # noqa: B017  - Pydantic doğrulaması veya SimulationError
            cfg = demo_project.sim_config.model_copy(update=upd)
            type(cfg).model_validate(cfg.model_dump())
            run_simulation(demo_project, routes, cfg)
    cfg = demo_project.sim_config.model_copy(update={"warmup_s": 10 * 3600.0})   # warmup > duration
    try:
        r = run_simulation(demo_project, routes, cfg)
    except (SimulationError, ValueError):
        return
    assert r.kpis.completed == 0 and any("tamamlanan ürün yok" in n for n in r.notes)


def test_warmup_window_excludes_early_entities(demo_project):
    routes = compute_routes(demo_project.layout, derive_flow_edges(demo_project))
    cfg = demo_project.sim_config.model_copy(update={"warmup_s": 2 * 3600})
    r = run_simulation(demo_project, routes, cfg)
    base = run_simulation(demo_project, routes)
    assert r.kpis.completed < base.kpis.completed
    assert any("Isınma" in n for n in r.notes)
