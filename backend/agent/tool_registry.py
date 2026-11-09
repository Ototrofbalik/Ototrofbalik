"""[Gemini Integration Engineer] 18 tool'un tanımı + Pydantic arg modelleri + Gemini FunctionDeclaration üretimi.
Tool adları master prompt §5 ile birebir aynı. Handler'lar backend.services üzerinden çalışır.

Kurallar:
- Hesaplama YOK: her sayı domain modüllerinden / services'ten gelir; handler yalnız çağırır ve özetler.
- Layout'u kalıcı değiştirecek tool'lar (add/update_workstation, import_factory_layout, optimize ... apply_best)
  değişikliği UYGULAMAZ: `Proposal` döner; executor bunu NEEDS_APPROVAL + PendingChange'e çevirir.
- Domain modülü hazır değilse (NotImplementedError / ModuleNotReady) executor MODULE_NOT_READY hatası döner.
"""
from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from backend import services
from backend.schemas import (
    Facility,
    Layout,
    OptimizationConfig,
    Project,
    SimConfig,
    StationKind,
    Strict,
    VehicleType,
    Workstation,
)
from backend.schemas.simulation import ResourceConfig

# --------------------------------------------------------------------------- yardımcı tipler


class ToolError(Exception):
    """Handler'ın bilinçli hatası → ToolResult(status=ERROR, error_code=code)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


class ToolRejected(ToolError):
    """Geçersiz/izinsiz işlem → ToolResult(status=REJECTED)."""

    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(code, message)
        self.data = data or {}


@dataclass
class HandlerOutput:
    data: dict[str, Any]
    artifacts: list[str] = field(default_factory=list)


@dataclass
class Proposal:
    """Onay bekleyen layout değişikliği. `commit` YALNIZ approve_change içinde çağrılır."""
    project_id: str
    base_layout_id: str
    description: str
    data: dict[str, Any]
    commit: Callable[[], dict[str, Any]]


@dataclass
class ToolContext:
    """Oturum bağlamı: aktif proje, yapılandırılmış (kaydedilmemiş) simülasyon ayarı, çıktı dizini."""
    project_id: str | None = None
    artifact_dir: Path = Path("data/artifacts")
    sim_config: dict[str, SimConfig] = field(default_factory=dict)

    def pid(self, explicit: str | None) -> str:
        pid = explicit or self.project_id
        if not pid:
            raise ToolError("NO_PROJECT", "Aktif proje yok; önce project_id verin veya proje oluşturun.")
        return pid


Handler = Callable[[ToolContext, Any], "HandlerOutput | Proposal"]


@dataclass
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Handler
    mutates_layout: bool = False


# --------------------------------------------------------------------------- arg modelleri
class _Base(Strict):
    project_id: str | None = Field(default=None, description="Proje id; boşsa aktif proje.")


class CreateProjectArgs(Strict):
    name: str = Field(min_length=1, max_length=120, description="Proje adı.")
    project_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_\-]{1,64}$",
                                   description="İsteğe bağlı id (harf/rakam/-/_); boşsa addan üretilir.")
    description: str = ""
    from_demo: bool = Field(default=False, description="True: ÖRNEK/varsayımsal demo fabrikanın kopyasıyla başla.")
    width_m: float = Field(default=60.0, gt=0, le=1000, description="Boş proje için tesis genişliği (m).")
    height_m: float = Field(default=40.0, gt=0, le=1000, description="Boş proje için tesis yüksekliği (m).")


class ImportFactoryLayoutArgs(_Base):
    format: Literal["workstations_csv", "layout_json"] = Field(
        description="workstations_csv: istasyon CSV'si mevcut layout'a uygulanır; layout_json: tam Layout JSON.")
    content: str = Field(min_length=2, description="Dosya içeriği (metin).")


class _StationFields(Strict):
    name: str | None = None
    kind: StationKind | None = None
    width: float | None = Field(default=None, gt=0)
    length: float | None = Field(default=None, gt=0)
    rotation: Literal["0", "90", "180", "270"] | None = None
    processing_time_s: float | None = Field(default=None, ge=0)
    setup_time_s: float | None = Field(default=None, ge=0)
    capacity: int | None = Field(default=None, ge=1)
    requires_operator: bool | None = None


class AddWorkstationArgs(_Base):
    station_id: str = Field(min_length=1, max_length=64, description="Yeni istasyon id (benzersiz).")
    name: str
    kind: StationKind = StationKind.MACHINE
    x: float = Field(description="Sol-alt köşe x (m).")
    y: float = Field(description="Sol-alt köşe y (m).")
    width: float = Field(gt=0, description="Rotasyon 0'da x yönü (m).")
    length: float = Field(gt=0, description="Rotasyon 0'da y yönü (m).")
    rotation: Literal["0", "90", "180", "270"] = "0"
    processing_time_s: float = Field(default=60.0, ge=0)
    setup_time_s: float = Field(default=0.0, ge=0)
    capacity: int = Field(default=1, ge=1)
    requires_operator: bool = False


class UpdateWorkstationArgs(_Base, _StationFields):
    station_id: str = Field(min_length=1, description="Güncellenecek istasyon id.")
    x: float | None = Field(default=None, description="Yeni sol-alt x (m).")
    y: float | None = Field(default=None, description="Yeni sol-alt y (m).")


class ValidateLayoutArgs(_Base):
    layout_id: str | None = Field(default=None, description="Boşsa aktif layout.")


class CalculateMaterialFlowArgs(_Base):
    top_n: int = Field(default=10, ge=1, le=50, description="Döndürülecek en yüksek From-To hücresi sayısı.")


class CalculateRoutesArgs(_Base):
    mode: Literal["network", "euclidean", "manhattan"] = "network"


class CreateSpaghettiChartArgs(_Base):
    title: str = "Spaghetti diyagramı"


class ResourceArg(Strict):
    vehicle: VehicleType
    count: int = Field(ge=0)
    speed_mps: float = Field(gt=0)


class ConfigureSimulationArgs(_Base):
    duration_s: float | None = Field(default=None, gt=0)
    warmup_s: float | None = Field(default=None, ge=0)
    seed: int | None = None
    replications: int | None = Field(default=None, ge=1, le=100)
    arrival: Literal["poisson", "deterministic"] | None = None
    processing_variability: Literal["deterministic", "triangular"] | None = None
    resources: list[ResourceArg] | None = Field(default=None, description="Verilirse mevcut kaynakların yerine geçer.")


class RunSimulationArgs(_Base):
    scenario_name: str | None = None


class GetSimulationMetricsArgs(_Base):
    run_id: int | None = Field(default=None, description="Boşsa aktif layout'un en son koşusu.")


class AnalyzeBottlenecksArgs(_Base):
    run_id: int | None = None


class _OptArgs(_Base):
    algorithm: Literal["swap_search", "local_search", "simulated_annealing", "ortools_assignment"] = \
        "simulated_annealing"
    iterations: int = Field(default=400, ge=1, le=100000)
    seed: int = 7
    n_alternatives: int = Field(default=3, ge=1, le=10)
    target_distance_reduction_pct: float | None = Field(default=None, gt=0, lt=100)
    simulate_alternatives: bool = True


class GenerateLayoutAlternativesArgs(_OptArgs):
    pass


class OptimizeFactoryLayoutArgs(_OptArgs):
    apply_best: bool = Field(default=False, description="True: en iyi alternatifi uygulama ÖNERİSİ oluşturur "
                                                        "(kullanıcı onayı gerekir; kayıtlı layout onaysız değişmez).")


class _ScenarioSel(_Base):
    baseline_run_id: int | None = None
    scenario_run_id: int | None = None
    optimization_id: int | None = Field(default=None, description="Boşsa en son optimizasyon.")
    alternative_id: str | None = Field(default=None, description="Boşsa en iyi alternatif.")


class CompareScenariosArgs(_ScenarioSel):
    pass


class CalculateSavingsArgs(_ScenarioSel):
    pass


class GenerateEngineeringReportArgs(_Base):
    format: Literal["html", "pdf"] = "html"
    ai_summary: str | None = Field(default=None, max_length=4000,
                                   description="İsteğe bağlı anlatım metni; sayılar rapor motorundan gelir.")


class ExportExcelReportArgs(_Base):
    pass


# --------------------------------------------------------------------------- yardımcılar
def _dump(model: BaseModel, **kw: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", **kw)


def _project(ctx: ToolContext, explicit: str | None) -> tuple[str, Project]:
    pid = ctx.pid(explicit)
    return pid, services.get_project(pid)


def _slug(name: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")
    return "-".join(p for p in out.split("-") if p)[:48] or "proje"


def _sim_summary(sim: Any) -> dict[str, Any]:
    return {"layout_id": sim.layout_id, "seed": sim.seed, "replications": sim.config.replications,
            "duration_s": sim.config.duration_s, "warmup_s": sim.config.warmup_s,
            "kpis": _dump(sim.kpis), "ci95": {k: _dump(v) for k, v in sim.ci95.items()},
            "stations": [_dump(s) for s in sim.stations], "resources": [_dump(r) for r in sim.resources],
            "bottleneck_station_id": sim.bottleneck_station_id, "not_modelled": list(sim.not_modelled),
            "notes": list(sim.notes), "event_count": len(sim.events)}


def _layout_proposal(ctx: ToolContext, pid: str, build: Callable[[Layout], Layout], description: str,
                     extra: dict[str, Any] | None = None) -> Proposal:
    """Yeni layout'u kurar, doğrular; hatasız ise onay bekleyen Proposal döner. Geçersizse ToolRejected."""
    from backend.layout.validation import validate_layout

    base = services.get_layout(pid)
    try:
        new_layout = build(base)
    except (ValueError, KeyError) as exc:
        raise ToolRejected("INVALID_CHANGE", f"Değişiklik reddedildi: {exc}") from exc
    return _validated_proposal(pid, base, new_layout, validate_layout(new_layout), description, extra)


def _validated_proposal(pid: str, base: Layout, new_layout: Layout, report: Any, description: str,
                        extra: dict[str, Any] | None) -> Proposal:
    errors = [i for i in report.issues if i.severity.value == "error"]
    if errors:
        raise ToolRejected(
            "INVALID_LAYOUT", "Önerilen layout doğrulamadan geçmedi; değişiklik reddedildi.",
            {"issues": [_dump(i) for i in errors], "layout_id": new_layout.id})
    warnings = [_dump(i) for i in report.issues if i.severity.value != "error"]

    def commit() -> dict[str, Any]:
        saved = services.save_layout_version(pid, new_layout)
        return {"layout_id": saved.id, "version": saved.version, "parent_id": saved.parent_id}

    return Proposal(project_id=pid, base_layout_id=base.id, description=description,
                    data={"validation_warnings": warnings, **(extra or {})}, commit=commit)


# --------------------------------------------------------------------------- handler'lar
def _create_project(ctx: ToolContext, a: CreateProjectArgs) -> HandlerOutput:
    from backend.demo.demo_factory import build_demo_project

    pid = a.project_id or _slug(a.name)
    if any(p["id"] == pid for p in services.list_projects()):
        raise ToolError("PROJECT_EXISTS", f"'{pid}' id'li proje zaten var; mevcut proje ezilmez.")
    demo = build_demo_project()
    if a.from_demo:
        layout = demo.layout.model_copy(update={"id": f"{pid}-layout-v1"}, deep=True)
        project = demo.model_copy(update={"id": pid, "name": a.name, "description": a.description,
                                          "is_demo": False, "layout": layout}, deep=True)
    else:
        layout = Layout(id=f"{pid}-layout-v1", name="Layout v1",
                        facility=Facility(name=a.name, width=a.width_m, height=a.height_m),
                        workstations=[], corridors=[])
        project = Project(id=pid, name=a.name, description=a.description, layout=layout, products=[],
                          sim_config=SimConfig(resources=[
                              ResourceConfig(vehicle=VehicleType.OPERATOR, count=1, speed_mps=1.2),
                              ResourceConfig(vehicle=VehicleType.FORKLIFT, count=1, speed_mps=2.5)]),
                          data_label="Kullanıcı projesi")
    saved = services.save_project(project)
    ctx.project_id = saved.id
    return HandlerOutput({"project_id": saved.id, "name": saved.name, "layout_id": saved.layout.id,
                          "n_workstations": len(saved.layout.workstations), "from_demo": a.from_demo,
                          "data_label": saved.data_label})


def _import_layout(ctx: ToolContext, a: ImportFactoryLayoutArgs) -> Proposal:
    pid = ctx.pid(a.project_id)

    def build(base: Layout) -> Layout:
        try:
            if a.format == "workstations_csv":
                return services.import_workstations_csv(a.content, base)
            lay = Layout.model_validate_json(a.content)
            return lay.model_copy(update={"id": base.id, "parent_id": base.parent_id, "version": base.version})
        except services.ServiceError as exc:
            raise ValueError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - pydantic doğrulama hatası
            raise ValueError(f"Layout içeriği geçersiz: {str(exc)[:300]}") from exc

    return _layout_proposal(ctx, pid, build, f"Layout içe aktarımı ({a.format}); mevcut layout yeni sürüm olur.",
                            {"format": a.format})


def _add_ws(ctx: ToolContext, a: AddWorkstationArgs) -> Proposal:
    from backend.layout.editing import add_workstation

    pid = ctx.pid(a.project_id)
    ws = Workstation(id=a.station_id, name=a.name, kind=a.kind, x=a.x, y=a.y, width=a.width, length=a.length,
                     rotation=a.rotation, processing_time_s=a.processing_time_s, setup_time_s=a.setup_time_s,
                     capacity=a.capacity, requires_operator=a.requires_operator)
    return _layout_proposal(ctx, pid, lambda base: add_workstation(base, ws),
                            f"İstasyon ekle: {a.station_id} ({a.x}, {a.y})", {"station_id": a.station_id})


def _update_ws(ctx: ToolContext, a: UpdateWorkstationArgs) -> Proposal:
    from backend.layout.editing import move_workstation

    pid = ctx.pid(a.project_id)
    changes = a.model_dump(exclude_none=True, exclude={"project_id", "station_id", "x", "y", "rotation"})
    if a.x is None and a.y is None and a.rotation is None and not changes:
        raise ToolRejected("NO_CHANGE", "Güncellenecek alan verilmedi.")

    def build(base: Layout) -> Layout:
        ws = base.station(a.station_id)    # yoksa KeyError → reddedilir
        out = base
        if a.x is not None or a.y is not None or a.rotation is not None:
            out = move_workstation(out, a.station_id, a.x if a.x is not None else ws.x,
                                   a.y if a.y is not None else ws.y, a.rotation)
        if changes:
            if ws.fixed:
                raise ValueError(f"İstasyon '{a.station_id}' sabit (fixed); değiştirilemez.")
            out = out.model_copy(deep=True)
            out.workstations = [s.model_copy(update=changes) if s.id == a.station_id else s
                                for s in out.workstations]
        return out

    fields = a.model_dump(exclude_none=True, exclude={"project_id", "station_id"})
    return _layout_proposal(ctx, pid, build, f"İstasyon güncelle: {a.station_id} {fields}",
                            {"station_id": a.station_id, "changes": fields})


def _validate(ctx: ToolContext, a: ValidateLayoutArgs) -> HandlerOutput:
    rep = services.validate_layout_for(ctx.pid(a.project_id), a.layout_id)
    n_err = sum(1 for i in rep.issues if i.severity.value == "error")
    return HandlerOutput({**_dump(rep), "n_errors": n_err, "n_warnings": len(rep.issues) - n_err})


def _routes_for(project: Project, mode: str = "network") -> tuple[list[Any], Any, Any]:
    from backend.routing.engine import compute_flow_metrics, compute_routes, derive_flow_edges

    edges = project.flow_edges or derive_flow_edges(project)
    routes = compute_routes(project.layout, edges, mode)
    metrics = compute_flow_metrics(project.layout, edges, routes, project)
    return edges, routes, metrics


def _material_flow(ctx: ToolContext, a: CalculateMaterialFlowArgs) -> HandlerOutput:
    _, project = _project(ctx, a.project_id)
    _, _, m = _routes_for(project)
    cells = sorted(m.cells, key=lambda c: c.metres_per_hour, reverse=True)
    d = _dump(m, exclude={"cells"})
    d.update(n_cells=len(m.cells), top_cells=[_dump(c) for c in cells[: a.top_n]])
    return HandlerOutput(d)


def _routes(ctx: ToolContext, a: CalculateRoutesArgs) -> HandlerOutput:
    _, project = _project(ctx, a.project_id)
    _, rs, _ = _routes_for(project, a.mode)
    return HandlerOutput({"layout_id": rs.layout_id, "mode": rs.mode, "n_routes": len(rs.routes),
                          "routes": [{"from_id": r.from_id, "to_id": r.to_id, "vehicle": r.vehicle.value,
                                      "distance_m": r.distance_m, "reachable": r.reachable, "mode": r.mode}
                                     for r in rs.routes],
                          "unreachable": [list(u) for u in rs.unreachable]})


def _spaghetti(ctx: ToolContext, a: CreateSpaghettiChartArgs) -> HandlerOutput:
    from backend.spaghetti.chart import spaghetti_figure

    _, project = _project(ctx, a.project_id)
    _, rs, m = _routes_for(project)
    ctx.artifact_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.artifact_dir / f"spaghetti_{project.layout.id}.html"
    spaghetti_figure(project.layout, rs, m, title=a.title).write_html(str(path))
    return HandlerOutput({"layout_id": project.layout.id, "path": str(path),
                          "total_distance_m_per_hour": m.total_distance_m_per_hour}, [str(path)])


def _configure_sim(ctx: ToolContext, a: ConfigureSimulationArgs) -> HandlerOutput:
    pid, project = _project(ctx, a.project_id)
    cur = ctx.sim_config.get(pid, project.sim_config)
    upd = a.model_dump(exclude_none=True, exclude={"project_id", "resources"})
    data = {**cur.model_dump(), **upd}
    if a.resources is not None:
        data["resources"] = [ResourceConfig(**r.model_dump()).model_dump() for r in a.resources]
    try:
        cfg = SimConfig.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        raise ToolRejected("INVALID_SIM_CONFIG", f"Simülasyon ayarı geçersiz: {str(exc)[:300]}") from exc
    if cfg.warmup_s >= cfg.duration_s:
        raise ToolRejected("INVALID_SIM_CONFIG", "warmup_s, duration_s'den küçük olmalı.")
    ctx.sim_config[pid] = cfg
    return HandlerOutput({"project_id": pid, "sim_config": _dump(cfg),
                          "persisted": False, "note": "Ayar bu oturumda geçerlidir; kayıtlı proje değişmedi."})


def _run_sim(ctx: ToolContext, a: RunSimulationArgs) -> HandlerOutput:
    pid = ctx.pid(a.project_id)
    run_id, sim = services.run_simulation_for(pid, ctx.sim_config.get(pid), scenario_name=a.scenario_name)
    return HandlerOutput({"run_id": run_id, **_sim_summary(sim)})


def _get_sim(ctx: ToolContext, run_id: int | None, project_id: str | None) -> tuple[int | None, Any]:
    if run_id is not None:
        return run_id, services.get_simulation(run_id)
    sim = services.get_latest_sim(ctx.pid(project_id))
    if sim is None:
        raise ToolError("NO_SIMULATION", "Bu layout için simülasyon yok; önce run_simpy_simulation çağırın.")
    return None, sim


def _sim_metrics(ctx: ToolContext, a: GetSimulationMetricsArgs) -> HandlerOutput:
    run_id, sim = _get_sim(ctx, a.run_id, a.project_id)
    return HandlerOutput({"run_id": run_id, **_sim_summary(sim)})


def _bottlenecks(ctx: ToolContext, a: AnalyzeBottlenecksArgs) -> HandlerOutput:
    from backend.analytics.bottleneck import analyze_bottlenecks

    run_id, sim = _get_sim(ctx, a.run_id, a.project_id)
    return HandlerOutput({"run_id": run_id, "layout_id": sim.layout_id, "analysis": analyze_bottlenecks(sim)})


def _opt_config(a: _OptArgs) -> OptimizationConfig:
    return OptimizationConfig(
        algorithm=a.algorithm, iterations=a.iterations, seed=a.seed, n_alternatives=a.n_alternatives,
        target_distance_reduction_pct=a.target_distance_reduction_pct, simulate_alternatives=a.simulate_alternatives)


def _opt_summary(opt_id: int, res: Any) -> dict[str, Any]:
    return {"optimization_id": opt_id, "baseline_layout_id": res.baseline_layout_id,
            "baseline_distance_m_per_hour": res.baseline_flow.total_distance_m_per_hour,
            "best_alternative_id": res.best_alternative_id, "target_met": res.target_met,
            "rationale": res.rationale, "infeasibility_notes": list(res.infeasibility_notes),
            "alternatives": [{"id": x.id, "name": x.name, "algorithm": x.algorithm, "objective": x.objective,
                              "distance_reduction_pct": x.distance_reduction_pct,
                              "total_distance_m_per_hour": x.flow.total_distance_m_per_hour,
                              "moved_station_ids": x.moved_station_ids, "valid": x.validation.valid,
                              "simulated": x.sim is not None,
                              "throughput_per_hour": x.sim.kpis.throughput_per_hour if x.sim else None}
                             for x in res.alternatives]}


def _gen_alts(ctx: ToolContext, a: GenerateLayoutAlternativesArgs) -> HandlerOutput:
    opt_id, res = services.run_optimization_for(ctx.pid(a.project_id), _opt_config(a))
    return HandlerOutput(_opt_summary(opt_id, res))


def _optimize(ctx: ToolContext, a: OptimizeFactoryLayoutArgs) -> HandlerOutput | Proposal:
    pid = ctx.pid(a.project_id)
    opt_id, res = services.run_optimization_for(pid, _opt_config(a))
    summary = _opt_summary(opt_id, res)
    if not a.apply_best:
        return HandlerOutput({**summary, "applied": False,
                              "note": "Alternatif uygulanmadı; uygulamak için apply_best=true ile onay isteyin."})
    best = res.best_alternative_id
    if best is None:
        raise ToolRejected("NO_FEASIBLE_ALTERNATIVE", "Uygulanabilir alternatif yok; uygulanacak değişiklik yok.",
                           summary)
    alt = next(x for x in res.alternatives if x.id == best)
    base = services.get_layout(pid)

    def commit() -> dict[str, Any]:
        saved = services.apply_optimization_alternative(pid, opt_id, best)
        return {"layout_id": saved.id, "version": saved.version, "parent_id": saved.parent_id,
                "optimization_id": opt_id, "alternative_id": best}

    if not alt.validation.valid:
        raise ToolRejected("INVALID_LAYOUT", "En iyi alternatif doğrulamadan geçmedi; uygulanamaz.", summary)
    return Proposal(project_id=pid, base_layout_id=base.id,
                    description=f"Optimizasyon alternatifini uygula: {best} "
                                f"(taşınan istasyonlar: {', '.join(alt.moved_station_ids) or '-'})",
                    data={**summary, "applied": False}, commit=commit)


def _scenario_pair(ctx: ToolContext, a: _ScenarioSel) -> tuple[Any, Any, Any, Any, Project, dict[str, Any]]:
    pid, project = _project(ctx, a.project_id)
    if a.baseline_run_id is not None or a.scenario_run_id is not None:
        if a.baseline_run_id is None or a.scenario_run_id is None:
            raise ToolError("INVALID_ARGS", "baseline_run_id ve scenario_run_id birlikte verilmeli.")
        return (services.get_simulation(a.baseline_run_id), services.get_simulation(a.scenario_run_id),
                None, None, project, {"baseline_run_id": a.baseline_run_id, "scenario_run_id": a.scenario_run_id})
    if a.optimization_id is not None:
        opt_id, res = a.optimization_id, services.get_optimization(a.optimization_id)
    else:
        latest = services.get_latest_optimization(pid)
        if latest is None:
            raise ToolError("NO_OPTIMIZATION", "Kayıtlı optimizasyon yok; önce optimize_factory_layout çağırın.")
        opt_id, res = latest
    alt_id = a.alternative_id or res.best_alternative_id
    alt = next((x for x in res.alternatives if x.id == alt_id), None)
    if alt is None:
        raise ToolError("NO_ALTERNATIVE", "Karşılaştırılacak alternatif bulunamadı.")
    if res.baseline_sim is None or alt.sim is None:
        raise ToolError("NO_SIMULATION", "Alternatif veya baseline simüle edilmemiş; simulate_alternatives=true "
                                         "ile optimizasyonu yeniden çalıştırın.")
    return (res.baseline_sim, alt.sim, res.baseline_flow, alt.flow, project,
            {"optimization_id": opt_id, "alternative_id": alt_id})


def _compare(ctx: ToolContext, a: CompareScenariosArgs) -> HandlerOutput:
    from backend.analytics.bottleneck import compare_results

    b, s, bf, sf, project, ref = _scenario_pair(ctx, a)
    cmp = compare_results(b, s, bf, sf, project)
    return HandlerOutput({**ref, **_dump(cmp)})


def _savings(ctx: ToolContext, a: CalculateSavingsArgs) -> HandlerOutput:
    from backend.analytics.bottleneck import compare_results

    b, s, bf, sf, project, ref = _scenario_pair(ctx, a)
    if project.cost is None:
        raise ToolError("NO_COST_CONFIG", "Projede maliyet verisi (CostConfig) yok; tasarruf/ROI hesaplanamaz.")
    cmp = compare_results(b, s, bf, sf, project)
    return HandlerOutput({**ref, "savings": cmp.savings, "currency": project.cost.currency,
                          "costs_assumed": project.cost.assumed, "notes": cmp.notes})


def _report_ctx(ctx: ToolContext, project_id: str | None, ai_text: str | None) -> tuple[str, Project, dict[str, Any]]:
    from backend.reporting import builder

    pid, project = _project(ctx, project_id)
    sim = services.get_latest_sim(pid)
    if sim is None:
        raise ToolError("NO_SIMULATION", "Rapor için simülasyon gerekli; önce run_simpy_simulation çağırın.")
    latest = services.get_latest_optimization(pid)
    return pid, project, builder.build_report_context(project, sim, latest[1] if latest else None, ai_text)


def _report(ctx: ToolContext, a: GenerateEngineeringReportArgs) -> HandlerOutput:
    from backend.reporting import builder

    pid, project, rctx = _report_ctx(ctx, a.project_id, a.ai_summary)
    ctx.artifact_dir.mkdir(parents=True, exist_ok=True)
    path = (builder.export_html if a.format == "html" else builder.export_pdf)(rctx, ctx.artifact_dir)
    report_id = services.save_report(pid, a.format, str(path), title=f"{project.name} mühendislik raporu")
    return HandlerOutput({"report_id": report_id, "path": str(path), "format": a.format}, [str(path)])


def _excel(ctx: ToolContext, a: ExportExcelReportArgs) -> HandlerOutput:
    from backend.reporting import builder

    pid, project, rctx = _report_ctx(ctx, a.project_id, None)
    ctx.artifact_dir.mkdir(parents=True, exist_ok=True)
    path = builder.export_excel(rctx, ctx.artifact_dir)
    report_id = services.save_report(pid, "excel", str(path), title=f"{project.name} Excel raporu")
    return HandlerOutput({"report_id": report_id, "path": str(path), "format": "xlsx"}, [str(path)])


# --------------------------------------------------------------------------- kayıt defteri (master prompt §5)
def _spec(name: str, desc: str, model: type[BaseModel], handler: Handler, mutates: bool = False) -> ToolSpec:
    return ToolSpec(name, desc, model, handler, mutates)


_SPECS = [
    _spec("create_project", "Yeni proje oluşturur (boş veya ÖRNEK demo kopyası). Mevcut projeyi ezmez.",
          CreateProjectArgs, _create_project),
    _spec("import_factory_layout", "İstasyon CSV'si veya Layout JSON'u içe aktarma ÖNERİSİ oluşturur; "
          "kullanıcı onayı olmadan kayıtlı layout değişmez.", ImportFactoryLayoutArgs, _import_layout, True),
    _spec("add_workstation", "Layout'a istasyon ekleme ÖNERİSİ oluşturur (doğrulanır; onay gerekir).",
          AddWorkstationArgs, _add_ws, True),
    _spec("update_workstation", "İstasyonu taşıma/döndürme/parametre güncelleme ÖNERİSİ oluşturur "
          "(doğrulanır; onay gerekir).", UpdateWorkstationArgs, _update_ws, True),
    _spec("validate_layout", "Layout'u çakışma, sınır, güvenlik boşluğu ve koridor erişimi için doğrular.",
          ValidateLayoutArgs, _validate),
    _spec("calculate_material_flow", "From-To malzeme akışını ve D_total (m/saat) metriklerini hesaplar.",
          CalculateMaterialFlowArgs, _material_flow),
    _spec("calculate_routes", "Koridor ağı üzerinde istasyonlar arası rotaları ve mesafeleri hesaplar.",
          CalculateRoutesArgs, _routes),
    _spec("create_spaghetti_chart", "Mevcut layout için spaghetti diyagramı (HTML) üretir.",
          CreateSpaghettiChartArgs, _spaghetti),
    _spec("configure_simulation", "Bu oturum için simülasyon ayarlarını (süre, seed, replikasyon, kaynaklar) "
          "belirler; kayıtlı projeyi değiştirmez.", ConfigureSimulationArgs, _configure_sim),
    _spec("run_simpy_simulation", "SimPy simülasyonunu çalıştırır ve sonucu kaydeder.",
          RunSimulationArgs, _run_sim),
    _spec("get_simulation_metrics", "Kayıtlı bir simülasyon koşusunun KPI'larını getirir.",
          GetSimulationMetricsArgs, _sim_metrics),
    _spec("analyze_bottlenecks", "Simülasyon sonucundan darboğaz analizi yapar.",
          AnalyzeBottlenecksArgs, _bottlenecks),
    _spec("generate_layout_alternatives", "Optimizasyon motoruyla uygulanabilir alternatif layout'lar üretir "
          "(uygulamaz).", GenerateLayoutAlternativesArgs, _gen_alts),
    _spec("optimize_factory_layout", "Layout'u optimize eder; apply_best=true ise en iyi alternatifin "
          "uygulanması için onay ister.", OptimizeFactoryLayoutArgs, _optimize, True),
    _spec("compare_scenarios", "İki simülasyon koşusunu veya baseline ile bir alternatifi karşılaştırır.",
          CompareScenariosArgs, _compare),
    _spec("calculate_savings", "Baseline ve senaryo arasındaki taşıma maliyeti tasarrufunu hesaplar "
          "(maliyet verisi yoksa hata).", CalculateSavingsArgs, _savings),
    _spec("generate_engineering_report", "Mühendislik raporu (HTML/PDF) üretir.",
          GenerateEngineeringReportArgs, _report),
    _spec("export_excel_report", "Sonuçları Excel raporu olarak dışa aktarır.", ExportExcelReportArgs, _excel),
]

TOOLS: dict[str, ToolSpec] = {s.name: s for s in _SPECS}
TOOL_NAMES: tuple[str, ...] = tuple(TOOLS)


# --------------------------------------------------------------------------- FunctionDeclaration üretimi
def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            return _inline_refs(copy.deepcopy(defs[node["$ref"].rsplit("/", 1)[-1]]), defs)
        return {k: _inline_refs(v, defs) for k, v in node.items() if k not in ("title", "$defs")}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def parameters_schema(spec: ToolSpec) -> dict[str, Any]:
    """Pydantic şemasından $ref'siz, başlıksız JSON Schema."""
    raw = spec.args_model.model_json_schema()
    return _inline_refs(raw, raw.get("$defs", {}))


def function_declaration_dicts(names: list[str] | None = None) -> list[dict[str, Any]]:
    """SDK'dan bağımsız tanımlar (name, description, parameters_json_schema)."""
    return [{"name": s.name, "description": s.description, "parameters_json_schema": parameters_schema(s)}
            for n, s in TOOLS.items() if names is None or n in names]


def function_declarations(names: list[str] | None = None) -> list[Any]:
    """google-genai `types.FunctionDeclaration` listesi."""
    from google.genai import types

    return [types.FunctionDeclaration(name=d["name"], description=d["description"],
                                      parameters_json_schema=d["parameters_json_schema"])
            for d in function_declaration_dicts(names)]
