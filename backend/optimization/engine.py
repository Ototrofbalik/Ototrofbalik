"""[Optimization Engineer — Opus] Layout optimizasyonu."""
from backend.schemas import OptimizationConfig, OptimizationResult, Project


def optimize_layout(project: Project, config: OptimizationConfig | None = None) -> OptimizationResult:
    """Yalnız validate_layout'tan geçen (error'suz) alternatifler döner. Hedef: ağırlıklı ağ-mesafe.
    En az n_alternatives (kısıtlar izin vermezse infeasibility_notes ile gerekçe). simulate_alternatives=True ise
    her alternatif aynı seed ile (CRN) simülasyondan geçer."""
    raise NotImplementedError
