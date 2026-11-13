"""[Master sözleşmesi — DONDURULDU] Isı haritası verileri. Saf fonksiyonlar; tüm sayılar SimResult/FlowMetrics'ten.

Uygulayan: A ajanı (hesap). Figürler: backend/analytics/heatmap_figures.py (B ajanı).
"""
from __future__ import annotations

from pydantic import Field

from backend.schemas import FlowMetrics, Layout, SimResult
from backend.schemas.common import Strict


class TimeGrid(Strict):
    """İstasyon × zaman dilimi matrisi. values[i][j] = station_ids[i], [bin_edges_s[j], bin_edges_s[j+1])."""
    metric: str                         # "utilization" | "avg_queue" | "max_queue"
    unit: str                           # "oran (0–1)" | "adet"
    layout_id: str
    station_ids: list[str]
    station_names: list[str]
    bin_edges_s: list[float]            # len = n_bins + 1; son dilim kısa olabilir
    values: list[list[float]]           # satır = istasyon, sütun = dilim
    warmup_s: float = 0.0               # ısınma dilimleri arayüzde taranır
    replication_shown: int = 0          # olay logu yalnız replikasyon 0
    truncated: bool = False             # olay logu kesildiyse son dilimler eksik
    notes: list[str] = Field(default_factory=list)


class FromToMatrix(Strict):
    """İstasyon × istasyon hareket matrisi (FlowMetrics.cells'ten). Hücre yoksa 0 / None."""
    layout_id: str
    mode: str                           # FlowMetrics.mode: network | euclidean | manhattan
    station_ids: list[str]              # akış sırasına göre (ürün rotası), sonra kalanlar
    station_names: list[str]
    moves_per_hour: list[list[float]]
    distance_m: list[list[float | None]]
    metres_per_hour: list[list[float]]
    total_metres_per_hour: float        # == FlowMetrics.total_distance_m_per_hour
    notes: list[str] = Field(default_factory=list)


def station_time_utilization(sim: SimResult, layout: Layout, bin_s: float = 3600.0) -> TimeGrid:
    """Dilim içindeki işlem meşguliyeti / (kapasite × dilim uzunluğu). Tam ufukta ağırlıklı ortalama
    StationStats.utilization ile eşleşmeli (motorun tanımıyla aynı: setup dahil mi, warmup nasıl — motoru oku)."""
    raise NotImplementedError


def station_time_queue(sim: SimResult, layout: Layout, bin_s: float = 3600.0, stat: str = "avg") -> TimeGrid:
    """stat='avg': dilimde zaman ağırlıklı ortalama kuyruk; 'max': dilimdeki en büyük kuyruk.
    Kuyruk = QUEUE_ENTERED→PROCESS_STARTED aralığı (operatör bekleyen dahil); bu tanım notes'ta yazılır."""
    raise NotImplementedError


def from_to_matrix(layout: Layout, metrics: FlowMetrics, order: list[str] | None = None) -> FromToMatrix:
    raise NotImplementedError
