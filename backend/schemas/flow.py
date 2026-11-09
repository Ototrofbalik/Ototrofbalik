"""Proses akışı, From-To, rota sözleşmeleri."""
from __future__ import annotations

from pydantic import Field

from .common import Point, Strict, VehicleType


class RouteStep(Strict):
    station_id: str
    transport_in: VehicleType = VehicleType.OPERATOR   # bir önceki istasyondan bu adıma taşıma aracı (ilk adımda yok sayılır)
    processing_time_s: float | None = None             # None => Workstation değeri
    setup_time_s: float | None = None


class Product(Strict):
    id: str
    name: str
    route: list[RouteStep] = Field(min_length=1)
    arrival_rate_per_hour: float = Field(gt=0)
    batch_size: int = Field(default=1, ge=1)             # taşıma birimi başına ürün adedi
    max_wip: int | None = None


class FlowEdge(Strict):
    """From-To hareketi (ürün rotalarından türetilir veya elle girilir)."""
    from_id: str
    to_id: str
    vehicle: VehicleType
    frequency_per_hour: float = Field(ge=0)
    quantity_per_move: float = 1.0


class Route(Strict):
    """Gerçek ağ rotası (koridor grafı üzerinde A*/Dijkstra)."""
    from_id: str
    to_id: str
    vehicle: VehicleType
    points: list[Point]               # istasyon çıkış noktası -> koridor -> istasyon giriş noktası
    distance_m: float
    reachable: bool = True
    mode: str = "network"             # network | euclidean | manhattan  (yaklaşık modlar asla 'network' ile karıştırılmaz)


class RouteSet(Strict):
    layout_id: str
    mode: str = "network"
    routes: list[Route]
    unreachable: list[tuple[str, str, str]] = Field(default_factory=list)  # (from, to, vehicle)

    def get(self, a: str, b: str, v: VehicleType) -> Route | None:
        for r in self.routes:
            if r.from_id == a and r.to_id == b and r.vehicle == v:
                return r
        return None


class FromToCell(Strict):
    from_id: str
    to_id: str
    moves_per_hour: float
    distance_m: float
    metres_per_hour: float


class FlowMetrics(Strict):
    """D_total = Σ frequency_i × route_distance_i ; T = d / v."""
    layout_id: str
    mode: str
    total_distance_m_per_hour: float
    distance_per_unit_m: float
    operator_walking_m_per_hour: float
    forklift_m_per_hour: float
    agv_m_per_hour: float
    transport_time_s_per_hour: float
    cells: list[FromToCell]
    material_handling_cost_per_hour: float | None = None   # CostConfig yoksa None
