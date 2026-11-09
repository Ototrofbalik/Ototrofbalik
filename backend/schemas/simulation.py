"""Simülasyon sözleşmeleri."""
from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .common import Strict, VehicleType


class ResourceConfig(Strict):
    vehicle: VehicleType
    count: int = Field(ge=0)
    speed_mps: float = Field(gt=0)
    load_time_s: float = Field(default=10.0, ge=0)
    unload_time_s: float = Field(default=10.0, ge=0)


class CostConfig(Strict):
    """Opsiyonel. Yoksa ROI/tasarruf HESAPLANMAZ."""
    cost_per_meter: dict[VehicleType, float] = Field(default_factory=dict)
    labor_cost_per_hour: float | None = None
    currency: str = "TRY"
    assumed: bool = True   # demo verisi varsayımsal


class SimConfig(Strict):
    duration_s: float = Field(default=8 * 3600, gt=0)
    warmup_s: float = Field(default=0.0, ge=0)
    seed: int = 42
    replications: int = Field(default=1, ge=1)
    resources: list[ResourceConfig]
    arrival: str = "poisson"       # poisson | deterministic
    processing_variability: str = "deterministic"   # deterministic | triangular(±%20)


class EventType(StrEnum):
    ENTITY_CREATED = "entity_created"
    QUEUE_ENTERED = "queue_entered"
    PROCESS_STARTED = "process_started"
    PROCESS_FINISHED = "process_finished"
    TRANSPORT_REQUESTED = "transport_requested"
    TRANSPORT_STARTED = "transport_started"
    TRANSPORT_COMPLETED = "transport_completed"
    ENTITY_COMPLETED = "entity_completed"


class EventRecord(Strict):
    t: float
    type: EventType
    entity_id: str
    product_id: str
    station_id: str | None = None
    from_id: str | None = None
    to_id: str | None = None
    vehicle: VehicleType | None = None
    resource_id: str | None = None
    distance_m: float | None = None
    queue_len: int | None = None


class StationStats(Strict):
    station_id: str
    utilization: float            # 0..1, busy / (capacity*T)
    avg_queue_wait_s: float
    max_queue_len: int
    processed: int
    avg_process_s: float
    avg_setup_s: float


class ResourceStats(Strict):
    vehicle: VehicleType
    utilization: float
    distance_m: float
    busy_s: float
    empty_travel_m: float = 0.0   # boş seyir; modellenmediyse 0.0 ve docs'ta belirtilir


class SimKPIs(Strict):
    throughput_per_hour: float
    completed: int
    avg_cycle_time_s: float
    avg_lead_time_s: float
    avg_wip: float
    avg_queue_wait_s: float
    max_queue_len: int
    total_travel_distance_m: float
    total_travel_time_s: float
    # zaman ayrışımı (ürün başına ortalama, s)
    avg_processing_s: float
    avg_setup_s: float
    avg_transport_s: float
    avg_loadunload_s: float
    avg_resource_wait_s: float
    avg_queue_s: float


class CI(Strict):
    mean: float
    lo: float
    hi: float
    n: int


class SimResult(Strict):
    layout_id: str
    config: SimConfig
    seed: int
    kpis: SimKPIs
    stations: list[StationStats]
    resources: list[ResourceStats]
    events: list[EventRecord] = Field(default_factory=list)   # replikasyon 0 (çok büyükse kısaltma notu notes'ta)
    replication_kpis: list[SimKPIs] = Field(default_factory=list)
    ci95: dict[str, CI] = Field(default_factory=dict)         # kpi adı -> CI (replications>1)
    bottleneck_station_id: str | None = None
    not_modelled: list[str] = Field(default_factory=lambda: ["arıza", "trafik çakışması", "vardiya", "boş seyir"])
    notes: list[str] = Field(default_factory=list)
