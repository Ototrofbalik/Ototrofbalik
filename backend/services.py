"""Uygulama servis katmanı: Project/Layout/Run kalıcılığı + modülleri orkestre eden tek giriş noktası.

API (FastAPI), Streamlit ve Agent tool'ları YALNIZ buradan çağırır. Burada HESAPLAMA yapılmaz; domain
modülleri (layout, routing, simulation, optimization) lazy import ile çağrılır. Modül henüz hazır değilse
(NotImplementedError / ImportError) `ModuleNotReady` fırlatılır (API: 501).
"""
from __future__ import annotations

import csv
import io
import json
import uuid
from collections.abc import Callable
from typing import Any, TypeVar

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from backend.database import session_scope
from backend.models import (
    AgentTaskRow,
    CorridorRow,
    FacilityRow,
    LayoutRow,
    MovementEventRow,
    ObstacleRow,
    OptimizationResultRow,
    ProcessRouteRow,
    ProductRow,
    ProjectRow,
    ReportRow,
    SimulationMetricRow,
    SimulationRunRow,
    SimulationScenarioRow,
    WorkstationRow,
)
from backend.schemas import (
    Corridor,
    CostConfig,
    EventRecord,
    Facility,
    FlowEdge,
    Layout,
    Obstacle,
    OptimizationConfig,
    OptimizationResult,
    Product,
    Project,
    RouteStep,
    SimConfig,
    SimResult,
    ToolCallRecord,
    ValidationReport,
    Workstation,
)

T = TypeVar("T")


# --------------------------------------------------------------------------- hatalar
class ServiceError(Exception):
    """Servis katmanı temel hatası (kullanıcıya Türkçe mesaj)."""


class NotFoundError(ServiceError, KeyError):
    def __str__(self) -> str:  # KeyError repr'ını kullanma
        return str(self.args[0]) if self.args else "bulunamadı"


class ModuleNotReady(ServiceError):
    """Çağrılan domain modülü henüz uygulanmamış."""


class ImportDataError(ServiceError, ValueError):
    """İçe aktarılan dosya geçersiz."""


def _call_domain(label: str, fn: Callable[[], T]) -> T:
    try:
        return fn()
    except NotImplementedError as exc:
        raise ModuleNotReady(f"{label} modülü henüz uygulanmadı.") from exc
    except ImportError as exc:
        raise ModuleNotReady(f"{label} modülü yüklenemedi: {exc}") from exc


# --------------------------------------------------------------------------- layout kalıcılığı
def _write_layout_rows(s: Session, project_id: str, layout: Layout) -> None:
    data = layout.model_dump(mode="json")
    s.add(LayoutRow(id=layout.id, project_id=project_id, name=layout.name, version=layout.version,
                    parent_id=layout.parent_id, data=data))
    s.flush()
    f = layout.facility
    s.add(FacilityRow(project_id=project_id, layout_id=layout.id, name=f.name, width=f.width, height=f.height,
                      grid=f.grid, safety_gap_m=f.safety_gap_m, min_corridor_width_m=f.min_corridor_width_m,
                      station_access_max_m=f.station_access_max_m))
    for w in layout.workstations:
        s.add(WorkstationRow(layout_id=layout.id, station_id=w.id, name=w.name, kind=w.kind.value, x=w.x, y=w.y,
                             width=w.width, length=w.length, rotation=w.rotation.value, fixed=w.fixed,
                             data=w.model_dump(mode="json")))
    for c in layout.corridors:
        s.add(CorridorRow(layout_id=layout.id, corridor_id=c.id, width=c.width, one_way=c.one_way,
                          data=c.model_dump(mode="json")))
    for o in layout.obstacles:
        s.add(ObstacleRow(layout_id=layout.id, obstacle_id=o.id, kind=o.kind, data=o.model_dump(mode="json")))


def _store_layout(s: Session, project: ProjectRow, layout: Layout, *, activate: bool = True) -> Layout:
    """Layout'u sürümleyerek yazar; mevcut sürümler ASLA ezilmez/silinmez."""
    existing = s.get(LayoutRow, layout.id)
    if existing is not None and existing.project_id == project.id:
        if existing.data == layout.model_dump(mode="json"):
            if activate:
                project.active_layout_id = existing.id
            return Layout.model_validate(existing.data)
        new_id = f"{layout.id}-{uuid.uuid4().hex[:6]}"
        parent = layout.id
    elif existing is not None:  # başka projeye ait aynı id
        new_id = f"{project.id}-{layout.id}"
        if s.get(LayoutRow, new_id) is not None:
            new_id = f"{new_id}-{uuid.uuid4().hex[:6]}"
        parent = layout.parent_id
    else:
        new_id, parent = layout.id, layout.parent_id
    max_v = s.scalar(select(func.max(LayoutRow.version)).where(LayoutRow.project_id == project.id)) or 0
    version = layout.version if (new_id == layout.id and layout.version > max_v) else max_v + 1
    if parent is None and project.active_layout_id and project.active_layout_id != new_id:
        parent = project.active_layout_id
    stored = layout.model_copy(update={"id": new_id, "version": version, "parent_id": parent})
    _write_layout_rows(s, project.id, stored)
    if activate:
        project.active_layout_id = stored.id
    return stored


# --------------------------------------------------------------------------- projeler
def _project_from_rows(s: Session, row: ProjectRow, layout_id: str | None = None) -> Project:
    lid = layout_id or row.active_layout_id
    lrow = s.get(LayoutRow, lid) if lid else None
    if lrow is None or lrow.project_id != row.id:
        raise NotFoundError(f"Proje '{row.id}' için layout bulunamadı: {lid}")
    products = []
    for p in s.scalars(select(ProductRow).where(ProductRow.project_id == row.id).order_by(ProductRow.pk)):
        products.append(Product(
            id=p.product_id, name=p.name, arrival_rate_per_hour=p.arrival_rate_per_hour,
            batch_size=p.batch_size, max_wip=p.max_wip,
            route=[RouteStep(station_id=st.station_id, transport_in=st.transport_in,
                             processing_time_s=st.processing_time_s, setup_time_s=st.setup_time_s)
                   for st in p.steps],
        ))
    return Project(
        id=row.id, name=row.name, description=row.description, is_demo=row.is_demo,
        layout=Layout.model_validate(lrow.data), products=products,
        flow_edges=[FlowEdge.model_validate(e) for e in row.flow_edges or []],
        sim_config=SimConfig.model_validate(row.sim_config),
        cost=CostConfig.model_validate(row.cost) if row.cost else None,
        data_label=row.data_label,
    )


def get_project(project_id: str, layout_id: str | None = None) -> Project:
    """Projeyi aktif (veya verilen) layout sürümüyle döner."""
    with session_scope() as s:
        row = s.get(ProjectRow, project_id)
        if row is None:
            raise NotFoundError(f"Proje bulunamadı: {project_id}")
        return _project_from_rows(s, row, layout_id)


def save_project(project: Project) -> Project:
    """Projeyi oluşturur/günceller. Layout değiştiyse yeni sürüm olarak eklenir (eskiler korunur)."""
    with session_scope() as s:
        row = s.get(ProjectRow, project.id)
        if row is None:
            row = ProjectRow(id=project.id, name=project.name, sim_config={})
            s.add(row)
        row.name = project.name
        row.description = project.description
        row.is_demo = project.is_demo
        row.data_label = project.data_label
        row.sim_config = project.sim_config.model_dump(mode="json")
        row.cost = project.cost.model_dump(mode="json") if project.cost else None
        row.flow_edges = [e.model_dump(mode="json") for e in project.flow_edges]
        s.flush()
        for old in s.scalars(select(ProductRow).where(ProductRow.project_id == project.id)):
            s.delete(old)
        s.flush()
        for p in project.products:
            prow = ProductRow(project_id=project.id, product_id=p.id, name=p.name,
                              arrival_rate_per_hour=p.arrival_rate_per_hour, batch_size=p.batch_size,
                              max_wip=p.max_wip)
            for i, st in enumerate(p.route):
                prow.steps.append(ProcessRouteRow(seq=i, station_id=st.station_id,
                                                  transport_in=st.transport_in.value,
                                                  processing_time_s=st.processing_time_s,
                                                  setup_time_s=st.setup_time_s))
            s.add(prow)
        _store_layout(s, row, project.layout)
        s.flush()
        return _project_from_rows(s, row)


def list_projects() -> list[dict[str, Any]]:
    """Özet liste (tam Project yüklemeden)."""
    with session_scope() as s:
        out = []
        for r in s.scalars(select(ProjectRow).order_by(ProjectRow.created_at, ProjectRow.id)):
            n_layouts = s.scalar(select(func.count()).select_from(LayoutRow).where(LayoutRow.project_id == r.id))
            out.append({"id": r.id, "name": r.name, "description": r.description, "is_demo": r.is_demo,
                        "data_label": r.data_label, "active_layout_id": r.active_layout_id,
                        "layout_versions": n_layouts or 0, "updated_at": r.updated_at.isoformat()})
        return out


def ensure_demo_project() -> Project:
    """Demo projeyi (backend.demo.demo_factory) yoksa yükler; varsa mevcut halini (kullanıcı sürümleri dahil) döner."""
    with session_scope() as s:
        demo = s.scalar(select(ProjectRow).where(ProjectRow.is_demo.is_(True)).order_by(ProjectRow.created_at))
        if demo is not None:
            return _project_from_rows(s, demo)
    from backend.demo.demo_factory import build_demo_project

    return save_project(build_demo_project())


# --------------------------------------------------------------------------- layout sürümleri
def save_layout_version(project_id: str, layout: Layout, *, activate: bool = True) -> Layout:
    """Layout'u yeni sürüm olarak kaydeder (version=max+1; parent_id zinciri). Eski sürümler korunur."""
    with session_scope() as s:
        row = s.get(ProjectRow, project_id)
        if row is None:
            raise NotFoundError(f"Proje bulunamadı: {project_id}")
        # Aynı id + farklı içerik, veya aynı id + aynı içerik -> _store_layout karar verir.
        return _store_layout(s, row, layout, activate=activate)


def list_layout_versions(project_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        row = s.get(ProjectRow, project_id)
        if row is None:
            raise NotFoundError(f"Proje bulunamadı: {project_id}")
        rows = s.scalars(select(LayoutRow).where(LayoutRow.project_id == project_id).order_by(LayoutRow.version))
        return [{"id": r.id, "name": r.name, "version": r.version, "parent_id": r.parent_id,
                 "active": r.id == row.active_layout_id, "created_at": r.created_at.isoformat()} for r in rows]


def get_layout(project_id: str, layout_id: str | None = None) -> Layout:
    return get_project(project_id, layout_id).layout


def activate_layout_version(project_id: str, layout_id: str) -> Layout:
    with session_scope() as s:
        row = s.get(ProjectRow, project_id)
        if row is None:
            raise NotFoundError(f"Proje bulunamadı: {project_id}")
        lrow = s.get(LayoutRow, layout_id)
        if lrow is None or lrow.project_id != project_id:
            raise NotFoundError(f"Layout bulunamadı: {layout_id}")
        row.active_layout_id = layout_id
        return Layout.model_validate(lrow.data)


# --------------------------------------------------------------------------- simülasyon kayıtları
def _metric_rows(run_id: int, result: SimResult) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for k, v in result.kpis.model_dump().items():
        rows.append({"run_id": run_id, "scope": "kpi", "subject": "", "name": k, "value": float(v)})
    for st in result.stations:
        for k, v in st.model_dump().items():
            if k != "station_id":
                rows.append({"run_id": run_id, "scope": "station", "subject": st.station_id, "name": k,
                             "value": float(v)})
    for rs in result.resources:
        for k, v in rs.model_dump().items():
            if k != "vehicle":
                rows.append({"run_id": run_id, "scope": "resource", "subject": rs.vehicle.value, "name": k,
                             "value": float(v)})
    return rows


def record_simulation_run(project_id: str, result: SimResult, scenario_name: str | None = None) -> int:
    """SimResult'ı (olaylar movement_events'e, metrikler simulation_metrics'e) kaydeder; run id döner."""
    with session_scope() as s:
        if s.get(ProjectRow, project_id) is None:
            raise NotFoundError(f"Proje bulunamadı: {project_id}")
        lrow = s.get(LayoutRow, result.layout_id)
        if lrow is None or lrow.project_id != project_id:
            raise NotFoundError(f"Simülasyon layout'u projede kayıtlı değil: {result.layout_id}")
        scenario_id = None
        if scenario_name:
            sc = SimulationScenarioRow(project_id=project_id, layout_id=result.layout_id, name=scenario_name,
                                       config=result.config.model_dump(mode="json"))
            s.add(sc)
            s.flush()
            scenario_id = sc.id
        blob = result.model_dump(mode="json", exclude={"events"})
        run = SimulationRunRow(project_id=project_id, layout_id=result.layout_id, scenario_id=scenario_id,
                               seed=result.seed, replications=result.config.replications, result=blob,
                               event_count=len(result.events))
        s.add(run)
        s.flush()
        if result.events:
            s.execute(insert(MovementEventRow), [
                {"run_id": run.id, "seq": i, "t": e.t, "type": e.type.value, "entity_id": e.entity_id,
                 "product_id": e.product_id, "station_id": e.station_id, "from_id": e.from_id,
                 "to_id": e.to_id, "vehicle": e.vehicle.value if e.vehicle else None,
                 "resource_id": e.resource_id, "distance_m": e.distance_m, "queue_len": e.queue_len}
                for i, e in enumerate(result.events)
            ])
        metrics = _metric_rows(run.id, result)
        if metrics:
            s.execute(insert(SimulationMetricRow), metrics)
        return run.id


def _load_sim(s: Session, run: SimulationRunRow) -> SimResult:
    events = [
        EventRecord(t=e.t, type=e.type, entity_id=e.entity_id, product_id=e.product_id, station_id=e.station_id,
                    from_id=e.from_id, to_id=e.to_id, vehicle=e.vehicle, resource_id=e.resource_id,
                    distance_m=e.distance_m, queue_len=e.queue_len)
        for e in s.scalars(select(MovementEventRow).where(MovementEventRow.run_id == run.id)
                           .order_by(MovementEventRow.seq))
    ]
    return SimResult.model_validate({**run.result, "events": [e.model_dump() for e in events]})


def get_simulation(run_id: int) -> SimResult:
    with session_scope() as s:
        run = s.get(SimulationRunRow, run_id)
        if run is None:
            raise NotFoundError(f"Simülasyon kaydı bulunamadı: {run_id}")
        return _load_sim(s, run)


def get_latest_sim(project_id: str, layout_id: str | None = None) -> SimResult | None:
    """Projenin (verilen veya aktif layout için) en son simülasyonu; yoksa None."""
    with session_scope() as s:
        prow = s.get(ProjectRow, project_id)
        if prow is None:
            raise NotFoundError(f"Proje bulunamadı: {project_id}")
        lid = layout_id or prow.active_layout_id
        run = s.scalar(select(SimulationRunRow).where(SimulationRunRow.project_id == project_id,
                                                      SimulationRunRow.layout_id == lid)
                       .order_by(SimulationRunRow.id.desc()).limit(1))
        return _load_sim(s, run) if run else None


def list_simulation_runs(project_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        if s.get(ProjectRow, project_id) is None:
            raise NotFoundError(f"Proje bulunamadı: {project_id}")
        rows = s.scalars(select(SimulationRunRow).where(SimulationRunRow.project_id == project_id)
                         .order_by(SimulationRunRow.id))
        return [{"id": r.id, "layout_id": r.layout_id, "scenario_id": r.scenario_id, "seed": r.seed,
                 "replications": r.replications, "event_count": r.event_count,
                 "throughput_per_hour": r.result.get("kpis", {}).get("throughput_per_hour"),
                 "created_at": r.created_at.isoformat()} for r in rows]


# --------------------------------------------------------------------------- optimizasyon kayıtları
def save_optimization(project_id: str, result: OptimizationResult) -> int:
    with session_scope() as s:
        if s.get(ProjectRow, project_id) is None:
            raise NotFoundError(f"Proje bulunamadı: {project_id}")
        row = OptimizationResultRow(
            project_id=project_id, baseline_layout_id=result.baseline_layout_id,
            best_alternative_id=result.best_alternative_id, algorithm=result.config.algorithm,
            target_met=result.target_met, result=result.model_dump(mode="json"))
        s.add(row)
        s.flush()
        return row.id


def get_optimization(opt_id: int) -> OptimizationResult:
    with session_scope() as s:
        row = s.get(OptimizationResultRow, opt_id)
        if row is None:
            raise NotFoundError(f"Optimizasyon kaydı bulunamadı: {opt_id}")
        return OptimizationResult.model_validate(row.result)


def get_latest_optimization(project_id: str) -> tuple[int, OptimizationResult] | None:
    with session_scope() as s:
        if s.get(ProjectRow, project_id) is None:
            raise NotFoundError(f"Proje bulunamadı: {project_id}")
        row = s.scalar(select(OptimizationResultRow).where(OptimizationResultRow.project_id == project_id)
                       .order_by(OptimizationResultRow.id.desc()).limit(1))
        return (row.id, OptimizationResult.model_validate(row.result)) if row else None


def list_optimizations(project_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        if s.get(ProjectRow, project_id) is None:
            raise NotFoundError(f"Proje bulunamadı: {project_id}")
        rows = s.scalars(select(OptimizationResultRow).where(OptimizationResultRow.project_id == project_id)
                         .order_by(OptimizationResultRow.id))
        return [{"id": r.id, "baseline_layout_id": r.baseline_layout_id, "algorithm": r.algorithm,
                 "best_alternative_id": r.best_alternative_id, "target_met": r.target_met,
                 "created_at": r.created_at.isoformat()} for r in rows]


def apply_optimization_alternative(project_id: str, opt_id: int, alternative_id: str) -> Layout:
    """Seçilen alternatifi yeni layout sürümü olarak kaydeder ve aktif yapar (baseline korunur)."""
    result = get_optimization(opt_id)
    for alt in result.alternatives:
        if alt.id == alternative_id:
            layout = alt.layout.model_copy(update={"parent_id": result.baseline_layout_id})
            return save_layout_version(project_id, layout)
    raise NotFoundError(f"Alternatif bulunamadı: {alternative_id}")


# --------------------------------------------------------------------------- agent kayıtları
def record_agent_task(task_id: str, project_id: str | None = None, user_text: str = "",
                      reply_text: str | None = None, status: str = "running",
                      requests_used: int | None = None, tokens_used: int | None = None) -> None:
    """Agent görevini oluşturur / günceller (task_id ile idempotent)."""
    with session_scope() as s:
        row = s.get(AgentTaskRow, task_id)
        if row is None:
            row = AgentTaskRow(id=task_id, project_id=project_id, user_text=user_text, tool_calls=[])
            s.add(row)
        if project_id is not None:
            row.project_id = project_id
        if user_text:
            row.user_text = user_text
        if reply_text is not None:
            row.reply_text = reply_text
        row.status = status
        if requests_used is not None:
            row.requests_used = requests_used
        if tokens_used is not None:
            row.tokens_used = tokens_used


def record_tool_call(record: ToolCallRecord) -> None:
    """Tool çağrısını ilgili görevin kaydına ekler (görev yoksa oluşturulur)."""
    with session_scope() as s:
        row = s.get(AgentTaskRow, record.task_id)
        if row is None:
            row = AgentTaskRow(id=record.task_id, tool_calls=[])
            s.add(row)
        row.tool_calls = [*(row.tool_calls or []), record.model_dump(mode="json")]


def get_agent_task(task_id: str) -> dict[str, Any]:
    with session_scope() as s:
        row = s.get(AgentTaskRow, task_id)
        if row is None:
            raise NotFoundError(f"Agent görevi bulunamadı: {task_id}")
        return {"id": row.id, "project_id": row.project_id, "user_text": row.user_text,
                "reply_text": row.reply_text, "status": row.status, "requests_used": row.requests_used,
                "tokens_used": row.tokens_used, "tool_calls": list(row.tool_calls or []),
                "created_at": row.created_at.isoformat()}


def list_agent_tasks(project_id: str | None = None) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = select(AgentTaskRow).order_by(AgentTaskRow.created_at, AgentTaskRow.id)
        if project_id:
            q = q.where(AgentTaskRow.project_id == project_id)
        return [{"id": r.id, "project_id": r.project_id, "status": r.status, "user_text": r.user_text,
                 "n_tool_calls": len(r.tool_calls or [])} for r in s.scalars(q)]


# --------------------------------------------------------------------------- raporlar
def save_report(project_id: str, kind: str, path: str, title: str = "", meta: dict[str, Any] | None = None) -> int:
    with session_scope() as s:
        if s.get(ProjectRow, project_id) is None:
            raise NotFoundError(f"Proje bulunamadı: {project_id}")
        row = ReportRow(project_id=project_id, kind=kind, title=title, path=path, meta=meta or {})
        s.add(row)
        s.flush()
        return row.id


def list_reports(project_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        if s.get(ProjectRow, project_id) is None:
            raise NotFoundError(f"Proje bulunamadı: {project_id}")
        rows = s.scalars(select(ReportRow).where(ReportRow.project_id == project_id).order_by(ReportRow.id))
        return [{"id": r.id, "kind": r.kind, "title": r.title, "path": r.path, "meta": r.meta,
                 "created_at": r.created_at.isoformat()} for r in rows]


# --------------------------------------------------------------------------- domain orkestrasyonu (lazy)
def validate_layout_for(project_id: str, layout_id: str | None = None) -> ValidationReport:
    layout = get_layout(project_id, layout_id)

    def _run() -> ValidationReport:
        from backend.layout.validation import validate_layout

        return validate_layout(layout)

    return _call_domain("Layout doğrulama", _run)


def run_simulation_for(project_id: str, config: SimConfig | None = None, layout_id: str | None = None,
                       scenario_name: str | None = None) -> tuple[int, SimResult]:
    """Rotaları hesaplar, simülasyonu çalıştırır ve sonucu kaydeder. (run_id, SimResult) döner."""
    project = get_project(project_id, layout_id)

    def _run() -> SimResult:
        from backend.routing.engine import compute_routes, derive_flow_edges
        from backend.simulation.engine import run_simulation

        edges = project.flow_edges or derive_flow_edges(project)
        routes = compute_routes(project.layout, edges)
        return run_simulation(project, routes, config)

    result = _call_domain("Simülasyon/rotalama", _run)
    return record_simulation_run(project_id, result, scenario_name), result


def run_optimization_for(project_id: str, config: OptimizationConfig | None = None) -> tuple[int, OptimizationResult]:
    project = get_project(project_id)

    def _run() -> OptimizationResult:
        from backend.optimization.engine import optimize_layout

        return optimize_layout(project, config)

    result = _call_domain("Optimizasyon", _run)
    return save_optimization(project_id, result), result


# --------------------------------------------------------------------------- içe / dışa aktarma
_WS_COLUMNS = ["id", "name", "kind", "x", "y", "width", "length", "rotation", "fixed", "input_x", "input_y",
               "output_x", "output_y", "processing_time_s", "setup_time_s", "capacity", "queue_capacity",
               "requires_operator"]
_EDGE_COLUMNS = ["from_id", "to_id", "vehicle", "frequency_per_hour", "quantity_per_move"]


def _ws_to_row(w: Workstation) -> dict[str, Any]:
    d = w.model_dump(mode="json")
    io_, oo = d.pop("input_offset"), d.pop("output_offset")
    d.update(input_x=io_["x"], input_y=io_["y"], output_x=oo["x"], output_y=oo["y"])
    return {c: d.get(c) for c in _WS_COLUMNS}


def _norm_scalar(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return None if v == "" else v
    return v


def _row_to_ws(r: dict[str, Any]) -> Workstation:
    d = {k: _norm_scalar(v) for k, v in r.items()}
    d = {k: v for k, v in d.items() if v is not None}
    if "rotation" in d and isinstance(d["rotation"], (int, float)):
        d["rotation"] = str(int(d["rotation"]))
    d["input_offset"] = {"x": d.pop("input_x", 0.0), "y": d.pop("input_y", 0.0)}
    d["output_offset"] = {"x": d.pop("output_x", 0.0), "y": d.pop("output_y", 0.0)}
    for k in ("fixed", "requires_operator"):
        if isinstance(d.get(k), str):
            d[k] = d[k].lower() in ("1", "true", "evet", "yes")
    return Workstation.model_validate(d)


def _csv_dump(columns: list[str], rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    wr = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n")
    wr.writeheader()
    for r in rows:
        wr.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in columns})
    return buf.getvalue()


def _csv_load(text: str) -> list[dict[str, Any]]:
    return [dict(r) for r in csv.DictReader(io.StringIO(text.lstrip("﻿")))]


def export_workstations_csv(layout: Layout) -> str:
    return _csv_dump(_WS_COLUMNS, [_ws_to_row(w) for w in layout.workstations])


def import_workstations_csv(text: str, base_layout: Layout) -> Layout:
    """CSV'deki istasyonlarla base_layout'un istasyonlarını değiştirir (diğer alanlar korunur). Kaydetmez."""
    rows = _csv_load(text)
    if not rows:
        raise ImportDataError("CSV boş: istasyon satırı yok.")
    try:
        stations = [_row_to_ws(r) for r in rows]
        return Layout.model_validate({**base_layout.model_dump(), "workstations":
                                      [w.model_dump() for w in stations]})
    except Exception as exc:
        raise ImportDataError(f"İstasyon CSV'si geçersiz: {exc}") from exc


def export_flow_edges_csv(edges: list[FlowEdge]) -> str:
    return _csv_dump(_EDGE_COLUMNS, [e.model_dump(mode="json") for e in edges])


def import_flow_edges_csv(text: str) -> list[FlowEdge]:
    try:
        return [FlowEdge.model_validate({k: v for k, v in
                                         ((k, _norm_scalar(v)) for k, v in r.items()) if v is not None})
                for r in _csv_load(text)]
    except Exception as exc:
        raise ImportDataError(f"Akış (From-To) CSV'si geçersiz: {exc}") from exc


def export_project_json(project_id: str) -> str:
    return get_project(project_id).model_dump_json(indent=2)


def import_project_json(text: str, *, new_id: str | None = None) -> Project:
    """JSON'dan Project içe aktarır ve kaydeder. Aynı id'li proje varsa mevcut proje EZİLMEZ: yeni id verilir."""
    try:
        project = Project.model_validate_json(text)
    except Exception as exc:
        raise ImportDataError(f"Proje JSON'u geçersiz: {exc}") from exc
    return _save_imported(project, new_id)


def _save_imported(project: Project, new_id: str | None) -> Project:
    with session_scope() as s:
        taken = s.get(ProjectRow, project.id) is not None
    if new_id or taken:
        project = project.model_copy(update={"id": new_id or f"{project.id}-{uuid.uuid4().hex[:6]}"})
    return save_project(project)


def _project_to_tables(p: Project) -> dict[str, list[dict[str, Any]]]:
    f = p.layout.facility
    meta = {
        "project_id": p.id, "name": p.name, "description": p.description, "is_demo": p.is_demo,
        "data_label": p.data_label, "layout_id": p.layout.id, "layout_name": p.layout.name,
        "layout_version": p.layout.version, "layout_parent_id": p.layout.parent_id,
        "sim_config": p.sim_config.model_dump_json(),
        "cost": p.cost.model_dump_json() if p.cost else None,
        **{f"facility_{k}": v for k, v in f.model_dump().items()},
    }
    return {
        "Meta": [{"key": k, "value": v} for k, v in meta.items()],
        "Istasyonlar": [_ws_to_row(w) for w in p.layout.workstations],
        "Koridorlar": [{"id": c.id, "width": c.width, "one_way": c.one_way,
                        "allowed_vehicles": ",".join(v.value for v in c.allowed_vehicles),
                        "points": json.dumps([[pt.x, pt.y] for pt in c.points])} for c in p.layout.corridors],
        "Engeller": [{"id": o.id, "name": o.name, "kind": o.kind, "x": o.rect.x, "y": o.rect.y,
                      "w": o.rect.w, "h": o.rect.h} for o in p.layout.obstacles],
        "Urunler": [{"id": pr.id, "name": pr.name, "arrival_rate_per_hour": pr.arrival_rate_per_hour,
                     "batch_size": pr.batch_size, "max_wip": pr.max_wip} for pr in p.products],
        "Rotalar": [{"product_id": pr.id, "seq": i, "station_id": st.station_id,
                     "transport_in": st.transport_in.value, "processing_time_s": st.processing_time_s,
                     "setup_time_s": st.setup_time_s} for pr in p.products for i, st in enumerate(pr.route)],
        "Akis": [e.model_dump(mode="json") for e in p.flow_edges],
    }


def _tables_to_project(t: dict[str, list[dict[str, Any]]]) -> Project:
    def clean(r: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in ((k, _norm_scalar(v)) for k, v in r.items()) if v is not None}

    def boolean(v: Any) -> bool:
        return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "evet", "yes")

    for name in ("Meta", "Istasyonlar", "Urunler", "Rotalar"):
        if not t.get(name):
            raise ImportDataError(f"Gerekli sayfa/tablo eksik veya boş: {name}")
    meta = {r["key"]: _norm_scalar(r.get("value")) for r in t["Meta"]}
    try:
        facility = Facility.model_validate({k[len("facility_"):]: v for k, v in meta.items()
                                            if k.startswith("facility_") and v is not None})
        corridors = []
        for r in t.get("Koridorlar", []):
            r = clean(r)
            corridors.append(Corridor.model_validate({
                "id": r["id"], "width": r.get("width", 3.0), "one_way": boolean(r.get("one_way", False)),
                "points": [{"x": x, "y": y} for x, y in json.loads(r["points"])],
                **({"allowed_vehicles": r["allowed_vehicles"].split(",")} if r.get("allowed_vehicles") else {}),
            }))
        obstacles = [Obstacle.model_validate({
            "id": r["id"], "name": r.get("name", "engel"), "kind": r.get("kind", "wall"),
            "rect": {"x": r["x"], "y": r["y"], "w": r["w"], "h": r["h"]}}) for r in map(clean, t.get("Engeller", []))]
        layout = Layout(
            id=meta["layout_id"], name=meta.get("layout_name") or "Layout", version=int(meta.get("layout_version") or 1),
            parent_id=meta.get("layout_parent_id"), facility=facility,
            workstations=[_row_to_ws(r) for r in t["Istasyonlar"]], corridors=corridors, obstacles=obstacles)
        steps: dict[str, list[tuple[int, RouteStep]]] = {}
        for r in map(clean, t["Rotalar"]):
            steps.setdefault(str(r["product_id"]), []).append((int(r["seq"]), RouteStep.model_validate(
                {k: v for k, v in r.items() if k not in ("product_id", "seq")})))
        products = []
        for r in map(clean, t["Urunler"]):
            route = [st for _, st in sorted(steps.get(str(r["id"]), []), key=lambda x: x[0])]
            products.append(Product.model_validate({
                "id": str(r["id"]), "name": r["name"], "route": [x.model_dump() for x in route],
                "arrival_rate_per_hour": r["arrival_rate_per_hour"], "batch_size": int(r.get("batch_size", 1)),
                "max_wip": int(r["max_wip"]) if r.get("max_wip") is not None else None}))
        return Project(
            id=meta["project_id"], name=meta.get("name") or "İçe aktarılan proje",
            description=meta.get("description") or "", is_demo=boolean(meta.get("is_demo", False)),
            data_label=meta.get("data_label") or "", layout=layout, products=products,
            flow_edges=[FlowEdge.model_validate(clean(r)) for r in t.get("Akis", [])],
            sim_config=SimConfig.model_validate_json(meta["sim_config"]),
            cost=CostConfig.model_validate_json(meta["cost"]) if meta.get("cost") else None)
    except ImportDataError:
        raise
    except Exception as exc:
        raise ImportDataError(f"Tablo verisi geçersiz: {exc}") from exc


def export_project_excel(project_id: str) -> bytes:
    """Projeyi çok sayfalı .xlsx olarak döner (sayfalar: Meta, Istasyonlar, Koridorlar, Engeller, Urunler, Rotalar, Akis)."""
    from openpyxl import Workbook

    tables = _project_to_tables(get_project(project_id))
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in tables.items():
        ws = wb.create_sheet(name)
        cols = list(rows[0].keys()) if rows else (_EDGE_COLUMNS if name == "Akis" else [])
        if rows:
            cols = list(dict.fromkeys(c for r in rows for c in r))
        ws.append(cols)
        for r in rows:
            ws.append([r.get(c) for c in cols])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def import_project_excel(data: bytes, *, new_id: str | None = None) -> Project:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), data_only=True)
    except Exception as exc:
        raise ImportDataError(f"Excel dosyası okunamadı: {exc}") from exc
    tables: dict[str, list[dict[str, Any]]] = {}
    for ws in wb.worksheets:
        it = ws.iter_rows(values_only=True)
        header = next(it, None)
        if not header:
            tables[ws.title] = []
            continue
        tables[ws.title] = [dict(zip(header, row, strict=False)) for row in it if any(c is not None for c in row)]
    return _save_imported(_tables_to_project(tables), new_id)

