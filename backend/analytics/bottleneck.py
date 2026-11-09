"""[SimPy Engineer] Darboğaz analizi ve senaryo karşılaştırması."""
from backend.schemas import Project, ScenarioComparison, SimResult


def analyze_bottlenecks(result: SimResult) -> dict:
    """Utilization + kuyruk bekleme + max kuyruk üzerinden sıralı darboğaz listesi (hesaplanmış sayılarla)."""
    raise NotImplementedError


def compare_results(baseline: SimResult, scenario: SimResult, baseline_flow=None, scenario_flow=None, project: Project | None = None) -> ScenarioComparison:
    """KPI deltaları; tasarruf yalnız project.cost varsa."""
    raise NotImplementedError
