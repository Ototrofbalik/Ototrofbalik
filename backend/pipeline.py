"""[Master Architect] Uçtan uca "Bu tesisi analiz et" boru hattı. Saf fonksiyon: DB'ye yazmaz.
Tüm adımlar gerçek domain modüllerini çağırır; hiçbir sayı burada üretilmez.
Gemini'siz de çalışır (Gemini yalnız anlatım ekler)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field

from backend.analytics.bottleneck import analyze_bottlenecks, compare_results
from backend.layout.validation import validate_layout
from backend.optimization.engine import optimize_layout
from backend.routing.engine import compute_flow_metrics, compute_routes, derive_flow_edges
from backend.schemas import (
    FlowMetrics,
    OptimizationConfig,
    OptimizationResult,
    Project,
    RouteSet,
    ScenarioComparison,
    SimResult,
    Strict,
    ValidationReport,
)
from backend.spaghetti.chart import spaghetti_figure


class StepLog(Strict):
    step: str
    ok: bool
    detail: str = ""


class FacilityAnalysis(Strict):
    project_id: str
    validation: ValidationReport
    flow: FlowMetrics | None = None
    routes: RouteSet | None = None
    baseline_sim: SimResult | None = None
    bottlenecks: dict[str, Any] = Field(default_factory=dict)
    optimization: OptimizationResult | None = None
    comparison: ScenarioComparison | None = None   # baseline vs en iyi alternatif
    artifacts: list[str] = Field(default_factory=list)
    steps: list[StepLog] = Field(default_factory=list)


def analyze_facility(project: Project, optimize: bool = True, opt_config: OptimizationConfig | None = None,
                     out_dir: Path | None = None) -> FacilityAnalysis:
    val = validate_layout(project.layout)
    res = FacilityAnalysis(project_id=project.id, validation=val)
    steps = res.steps
    steps.append(StepLog(step="validate_layout", ok=val.valid, detail=f"{len(val.issues)} bulgu"))
    if not val.valid:
        return res  # geçersiz layout ile analiz yapılmaz

    edges = project.flow_edges or derive_flow_edges(project)
    res.routes = compute_routes(project.layout, edges)
    res.flow = compute_flow_metrics(project.layout, edges, res.routes, project)
    steps.append(StepLog(step="material_flow_routes", ok=True,
                         detail=f"D_total={res.flow.total_distance_m_per_hour:.1f} m/sa"))
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        fig = spaghetti_figure(project.layout, res.routes, res.flow, title="Mevcut durum")
        p = out_dir / f"spaghetti_{project.layout.id}.html"
        fig.write_html(str(p))
        res.artifacts.append(str(p))
        steps.append(StepLog(step="spaghetti_chart", ok=True, detail=p.name))

    res.baseline_sim = _sim(project, res.routes)
    steps.append(StepLog(step="simulation", ok=True,
                         detail=f"throughput={res.baseline_sim.kpis.throughput_per_hour:.2f}/sa, seed={res.baseline_sim.seed}"))
    res.bottlenecks = analyze_bottlenecks(res.baseline_sim)
    steps.append(StepLog(step="bottleneck_analysis", ok=True, detail=str(res.bottlenecks.get("bottleneck_station_id"))))

    if optimize:
        res.optimization = optimize_layout(project, opt_config)
        steps.append(StepLog(step="optimization", ok=True, detail=f"{len(res.optimization.alternatives)} alternatif"))
        best = next((a for a in res.optimization.alternatives if a.id == res.optimization.best_alternative_id), None)
        if best is not None and best.sim is not None:
            res.comparison = compare_results(res.baseline_sim, best.sim, res.flow, best.flow, project)
            steps.append(StepLog(step="scenario_comparison", ok=True, detail=f"en iyi: {best.id}"))
    return res


def _sim(project: Project, routes: RouteSet) -> SimResult:
    from backend.simulation.engine import run_simulation
    return run_simulation(project, routes)
