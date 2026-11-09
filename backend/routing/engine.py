"""[Layout&Routing Engineer] Koridor ağı + A*/Dijkstra rotalama."""
from backend.schemas import FlowEdge, FlowMetrics, Layout, Project, RouteSet, VehicleType


def derive_flow_edges(project: Project) -> list[FlowEdge]:
    """Ürün rotalarından From-To kenarları: frequency = arrival_rate / batch_size (adım geçişi başına)."""
    raise NotImplementedError


def compute_routes(layout: Layout, edges: list[FlowEdge], mode: str = "network") -> RouteSet:
    """mode='network': koridor grafı (networkx) üzerinde Dijkstra/A*; engellerden geçmez; tek yön ve
    allowed_vehicles uyulur; operatör serbest zeminde yürüyebilir (koridor dışı erişim kısa stub'ları ile).
    mode='euclidean'|'manhattan': yalnız basit analiz; Route.mode bunu işaretler."""
    raise NotImplementedError


def compute_flow_metrics(layout: Layout, edges: list[FlowEdge], routes: RouteSet, project: Project | None = None) -> FlowMetrics:
    """D_total = Σ freq_i × dist_i ; T = dist/speed(project.sim_config.resources)."""
    raise NotImplementedError
