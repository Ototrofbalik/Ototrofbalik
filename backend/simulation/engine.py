"""[SimPy Engineer] Gerçek SimPy ayrık olay simülasyonu."""
from backend.schemas import Project, RouteSet, SimConfig, SimResult


def run_simulation(project: Project, routes: RouteSet, config: SimConfig | None = None) -> SimResult:
    """Rota mesafeleri `routes` içinden (gerçek ağ rotası) gelir. Replikasyonlar seed+i ile; CRN: aynı seed
    iki senaryoda aynı varış/işlem süresi akışını üretmelidir (varış ve süre akışları ayrı RNG'ler).
    Kaynaklar simpy.Resource/Store; olay logu tüm EventType'ları içerir."""
    raise NotImplementedError
