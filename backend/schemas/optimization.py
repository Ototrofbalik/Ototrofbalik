"""Optimizasyon / senaryo sözleşmeleri."""
from __future__ import annotations

from pydantic import Field

from .common import Strict
from .flow import FlowMetrics
from .layout import Layout, ValidationReport
from .simulation import SimResult


class ObjectiveWeights(Strict):
    distance: float = 1.0       # ağ-mesafe (D_total)
    walking: float = 0.5        # operatör yürüyüşü
    adjacency: float = 0.2      # yakınlık gereksinimleri cezası
    area: float = 0.0


class OptimizationConfig(Strict):
    algorithm: str = "simulated_annealing"   # swap_search | local_search | simulated_annealing | ortools_assignment
    iterations: int = Field(default=400, ge=1)
    seed: int = 7
    n_alternatives: int = Field(default=3, ge=1, le=10)
    weights: ObjectiveWeights = Field(default_factory=ObjectiveWeights)
    target_distance_reduction_pct: float | None = None
    adjacency_pairs: list[tuple[str, str]] = Field(default_factory=list)   # yakın olması istenen çiftler
    simulate_alternatives: bool = True


class LayoutAlternative(Strict):
    id: str
    name: str
    algorithm: str
    layout: Layout
    validation: ValidationReport
    objective: float
    flow: FlowMetrics
    sim: SimResult | None = None
    distance_reduction_pct: float          # baseline'a göre (flow.total_distance)
    moved_station_ids: list[str]


class OptimizationResult(Strict):
    baseline_layout_id: str
    baseline_flow: FlowMetrics
    baseline_sim: SimResult | None = None
    alternatives: list[LayoutAlternative]
    best_alternative_id: str | None = None
    rationale: str = ""
    infeasibility_notes: list[str] = Field(default_factory=list)
    target_met: bool | None = None
    config: OptimizationConfig


class KpiDelta(Strict):
    name: str
    unit: str
    baseline: float
    scenario: float
    delta_abs: float
    delta_pct: float | None
    better: bool | None            # None = yön belirsiz


class ScenarioComparison(Strict):
    baseline_id: str
    scenario_id: str
    deltas: list[KpiDelta]
    same_seed: bool                # ortak rastgele sayı (CRN) kullanıldı mı
    seed: int
    replications: int
    savings: dict[str, float] | None = None    # yalnız CostConfig varsa dolu
    notes: list[str] = Field(default_factory=list)
