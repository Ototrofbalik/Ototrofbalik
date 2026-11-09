"""Servis katmanı: DB round-trip, sürümleme, kayıtlar, import/export.

Not: SimResult/OptimizationResult nesneleri burada YALNIZ kalıcılık round-trip'ini sınamak için elle kurulur
(KPI iddiası değildir)."""
from __future__ import annotations

import pytest

from backend import services
from backend.database import configure_database
from backend.demo.demo_factory import build_demo_project
from backend.schemas import (
    EventRecord,
    EventType,
    FlowMetrics,
    Layout,
    LayoutAlternative,
    OptimizationConfig,
    OptimizationResult,
    ResourceStats,
    SimKPIs,
    SimResult,
    StationStats,
    ToolCallRecord,
    ToolStatus,
    ValidationReport,
    VehicleType,
)


@pytest.fixture(autouse=True)
def db(tmp_path):
    configure_database(f"sqlite:///{tmp_path / 'test.db'}")
    yield


def _sim(layout_id: str, throughput: float = 5.0, n_events: int = 3) -> SimResult:
    cfg = build_demo_project().sim_config
    kpis = SimKPIs(
        throughput_per_hour=throughput, completed=10, avg_cycle_time_s=1.0, avg_lead_time_s=2.0, avg_wip=3.0,
        avg_queue_wait_s=4.0, max_queue_len=5, total_travel_distance_m=6.0, total_travel_time_s=7.0,
        avg_processing_s=1.0, avg_setup_s=0.0, avg_transport_s=1.0, avg_loadunload_s=1.0,
        avg_resource_wait_s=0.0, avg_queue_s=1.0)
    events = [EventRecord(t=float(i), type=EventType.TRANSPORT_STARTED, entity_id=f"e{i}", product_id="widget-a",
                          from_id="raw", to_id="cut", vehicle=VehicleType.FORKLIFT, distance_m=12.5 + i)
              for i in range(n_events)]
    return SimResult(
        layout_id=layout_id, config=cfg, seed=cfg.seed, kpis=kpis,
        stations=[StationStats(station_id="raw", utilization=0.5, avg_queue_wait_s=1.0, max_queue_len=2,
                               processed=10, avg_process_s=60.0, avg_setup_s=0.0)],
        resources=[ResourceStats(vehicle=VehicleType.FORKLIFT, utilization=0.3, distance_m=100.0, busy_s=50.0)],
        events=events, bottleneck_station_id="raw")


def test_demo_project_round_trip():
    demo = services.ensure_demo_project()
    assert demo.is_demo and demo.data_label
    again = services.get_project(demo.id)
    assert again == build_demo_project().model_copy(update={}) or again.model_dump() == demo.model_dump()
    assert again.layout == build_demo_project().layout
    assert again.products == build_demo_project().products
    assert again.cost == build_demo_project().cost
    # idempotent
    services.ensure_demo_project()
    assert len(services.list_projects()) == 1


def test_all_fifteen_tables_exist():
    from backend.database import Base

    expected = {"projects", "facilities", "layouts", "workstations", "corridors", "obstacles", "products",
                "process_routes", "simulation_scenarios", "simulation_runs", "movement_events",
                "simulation_metrics", "optimization_results", "agent_tasks", "reports"}
    assert expected == set(Base.metadata.tables)


def test_get_missing_project():
    with pytest.raises(services.NotFoundError):
        services.get_project("yok")


def test_layout_versioning_preserves_old_versions():
    demo = services.ensure_demo_project()
    base = demo.layout
    moved = base.model_copy(update={"workstations": [
        w.model_copy(update={"x": w.x + 1}) if w.id == "raw" else w for w in base.workstations]})
    # aynı id, farklı içerik -> yeni sürüm
    v2 = services.save_layout_version(demo.id, moved)
    assert v2.version == 2 and v2.id != base.id and v2.parent_id == base.id
    v3 = services.save_layout_version(demo.id, v2.model_copy(update={"name": "v3"}))
    assert v3.version == 3 and v3.parent_id == v2.id
    versions = services.list_layout_versions(demo.id)
    assert [v["version"] for v in versions] == [1, 2, 3]
    assert [v["active"] for v in versions] == [False, False, True]
    # eski sürüm bire bir korunur
    assert services.get_layout(demo.id, base.id) == base
    assert services.get_project(demo.id).layout.id == v3.id
    # aynı içeriği tekrar kaydetmek sürüm eklemez
    services.save_layout_version(demo.id, v3)
    assert len(services.list_layout_versions(demo.id)) == 3
    # aktif sürümü değiştir
    services.activate_layout_version(demo.id, base.id)
    assert services.get_project(demo.id).layout == base


def test_save_project_new_layout_adds_version_without_deleting():
    demo = services.ensure_demo_project()
    edited = demo.model_copy(update={"layout": demo.layout.model_copy(update={"name": "düzenlendi"})})
    saved = services.save_project(edited)
    assert saved.layout.name == "düzenlendi"
    assert len(services.list_layout_versions(demo.id)) == 2


def test_normalized_rows_match_blob():
    from sqlalchemy import func, select

    from backend.database import session_scope
    from backend.models import CorridorRow, FacilityRow, ObstacleRow, WorkstationRow

    demo = services.ensure_demo_project()
    with session_scope() as s:
        lid = demo.layout.id
        assert s.scalar(select(func.count()).select_from(WorkstationRow).where(WorkstationRow.layout_id == lid)) == 7
        assert s.scalar(select(func.count()).select_from(CorridorRow).where(CorridorRow.layout_id == lid)) == 2
        assert s.scalar(select(func.count()).select_from(ObstacleRow).where(ObstacleRow.layout_id == lid)) == 1
        assert s.scalar(select(FacilityRow.width).where(FacilityRow.layout_id == lid)) == 30


def test_simulation_record_and_latest():
    demo = services.ensure_demo_project()
    assert services.get_latest_sim(demo.id, demo.layout.id) is None
    r1 = _sim(demo.layout.id, 5.0, n_events=4)
    id1 = services.record_simulation_run(demo.id, r1, scenario_name="baseline")
    r2 = _sim(demo.layout.id, 6.0, n_events=2)
    services.record_simulation_run(demo.id, r2)
    latest = services.get_latest_sim(demo.id, demo.layout.id)
    assert latest == r2
    assert services.get_simulation(id1) == r1            # eski çalıştırma silinmedi
    assert len(services.list_simulation_runs(demo.id)) == 2
    with pytest.raises(services.NotFoundError):
        services.record_simulation_run(demo.id, _sim("başka-layout"))


def test_metrics_table_populated():
    from sqlalchemy import func, select

    from backend.database import session_scope
    from backend.models import SimulationMetricRow

    demo = services.ensure_demo_project()
    run_id = services.record_simulation_run(demo.id, _sim(demo.layout.id))
    with session_scope() as s:
        n = s.scalar(select(func.count()).select_from(SimulationMetricRow).where(SimulationMetricRow.run_id == run_id))
        thr = s.scalar(select(SimulationMetricRow.value).where(SimulationMetricRow.run_id == run_id,
                                                               SimulationMetricRow.name == "throughput_per_hour"))
    assert n > 15 and thr == 5.0


def test_optimization_round_trip_and_apply():
    demo = services.ensure_demo_project()
    base = demo.layout
    alt_layout = base.model_copy(update={"id": "alt-1", "name": "Alt 1", "version": 2, "workstations": [
        w.model_copy(update={"x": w.x + 0.5}) if w.id == "raw" else w for w in base.workstations]})
    flow = FlowMetrics(layout_id=base.id, mode="network", total_distance_m_per_hour=100.0, distance_per_unit_m=9.0,
                       operator_walking_m_per_hour=10.0, forklift_m_per_hour=90.0, agv_m_per_hour=0.0,
                       transport_time_s_per_hour=50.0, cells=[])
    alt = LayoutAlternative(id="alt-1", name="Alt 1", algorithm="swap_search", layout=alt_layout,
                            validation=ValidationReport(layout_id="alt-1", valid=True), objective=1.0, flow=flow,
                            distance_reduction_pct=10.0, moved_station_ids=["raw"])
    result = OptimizationResult(baseline_layout_id=base.id, baseline_flow=flow, alternatives=[alt],
                                best_alternative_id="alt-1", config=OptimizationConfig())
    oid = services.save_optimization(demo.id, result)
    assert services.get_optimization(oid) == result
    found = services.get_latest_optimization(demo.id)
    assert found is not None and found[0] == oid
    assert services.list_optimizations(demo.id)[0]["best_alternative_id"] == "alt-1"
    applied = services.apply_optimization_alternative(demo.id, oid, "alt-1")
    assert applied.parent_id == base.id and applied.version == 2
    assert services.get_layout(demo.id, base.id) == base
    with pytest.raises(services.NotFoundError):
        services.apply_optimization_alternative(demo.id, oid, "yok")


def test_agent_task_and_tool_calls():
    demo = services.ensure_demo_project()
    services.record_agent_task("t1", demo.id, user_text="Bu tesisi analiz et")
    for i, tool in enumerate(["get_layout", "run_simulation"]):
        services.record_tool_call(ToolCallRecord(task_id="t1", seq=i, tool=tool, args={"a": i},
                                                 status=ToolStatus.OK, duration_ms=1.5, output_summary="ok"))
    services.record_agent_task("t1", reply_text="bitti", status="done", requests_used=2, tokens_used=99)
    t = services.get_agent_task("t1")
    assert t["status"] == "done" and t["user_text"] == "Bu tesisi analiz et"
    assert [c["tool"] for c in t["tool_calls"]] == ["get_layout", "run_simulation"]
    assert t["tokens_used"] == 99
    assert services.list_agent_tasks(demo.id)[0]["n_tool_calls"] == 2


def test_reports():
    demo = services.ensure_demo_project()
    rid = services.save_report(demo.id, "html", "data/generated_reports/a.html", "Rapor", {"sections": 14})
    rows = services.list_reports(demo.id)
    assert rows[0]["id"] == rid and rows[0]["meta"] == {"sections": 14}


def test_json_export_import_round_trip():
    demo = services.ensure_demo_project()
    text = services.export_project_json(demo.id)
    imported = services.import_project_json(text)
    assert imported.id != demo.id                      # mevcut proje ezilmedi
    assert imported.layout.workstations == demo.layout.workstations
    assert imported.layout.id != demo.layout.id        # başka projeye ait layout id çakışması çözüldü
    assert len(services.list_projects()) == 2
    with pytest.raises(services.ImportDataError):
        services.import_project_json("{bozuk")


def test_csv_round_trip_and_errors():
    demo = services.ensure_demo_project()
    csv_text = services.export_workstations_csv(demo.layout)
    layout = services.import_workstations_csv(csv_text, demo.layout)
    assert layout.workstations == demo.layout.workstations
    with pytest.raises(services.ImportDataError):
        services.import_workstations_csv("id,name\n", demo.layout)
    with pytest.raises(services.ImportDataError):
        services.import_workstations_csv("id,name,x,y,width,length\na,A,1,2,-3,4\n", demo.layout)
    edges_csv = services.export_flow_edges_csv([])
    assert services.import_flow_edges_csv(edges_csv) == []
    from backend.schemas import FlowEdge

    e = [FlowEdge(from_id="raw", to_id="cut", vehicle=VehicleType.FORKLIFT, frequency_per_hour=11.0)]
    assert services.import_flow_edges_csv(services.export_flow_edges_csv(e)) == e


def test_excel_round_trip():
    pytest.importorskip("openpyxl")
    demo = services.ensure_demo_project()
    data = services.export_project_excel(demo.id)
    imported = services.import_project_excel(data)
    assert imported.id != demo.id
    assert imported.layout.workstations == demo.layout.workstations
    assert imported.layout.corridors == demo.layout.corridors
    assert imported.layout.obstacles == demo.layout.obstacles
    assert imported.layout.facility == demo.layout.facility
    assert imported.products == demo.products
    assert imported.sim_config == demo.sim_config
    assert imported.cost == demo.cost
    with pytest.raises(services.ImportDataError):
        services.import_project_excel(b"bu xlsx degil")


def test_domain_modules_not_ready_raise_module_not_ready():
    demo = services.ensure_demo_project()
    # Bu modüller henüz uygulanmamışsa ModuleNotReady; uygulanmışsa çağrı başarılı olmalı.
    for call in (lambda: services.validate_layout_for(demo.id),
                 lambda: services.run_simulation_for(demo.id),
                 lambda: services.run_optimization_for(demo.id)):
        try:
            call()
        except services.ModuleNotReady:
            pass


def test_layout_type_is_contract_layout():
    demo = services.ensure_demo_project()
    assert isinstance(services.get_layout(demo.id), Layout)
