"""[Master Architect] Digital Twin veri sözleşmesi (DONDURULDU). Sütunlu (columnar) JSON; koordinatlar m, süre s.
Değişiklik yalnız Master üzerinden. Ayrıntı: docs/twin_spec.md"""
from __future__ import annotations

from pydantic import Field

from backend.schemas.common import Strict

SCHEMA_VERSION = 1

# Varlık segment türleri (Segs.kind)
SEG_QUEUE, SEG_PROCESS, SEG_BLOCKED, SEG_VEH_WAIT, SEG_LOAD, SEG_TRAVEL, SEG_UNLOAD = range(7)
# Araç segment türleri (VSegs.kind)
VSEG_IDLE, VSEG_WORK, VSEG_LOAD, VSEG_TRAVEL, VSEG_UNLOAD = range(5)


class TwinMeta(Strict):
    layout_id: str
    scenario_label: str = "Mevcut"
    duration_s: float
    warmup_s: float = 0.0
    seed: int
    replications: int = 1
    replication_shown: int = 0
    t_end: float                      # oynatılabilir son an (kesik log değilse duration_s)
    truncated: bool = False
    t_valid_end: float | None = None  # kesik logda güvenilir son an
    bottleneck_station_id: str | None = None
    not_modelled: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    teleport_count: int = 0           # boş seyir modellenmediği için 'ışınlanan' görev sayısı
    vehicle_colors: dict[str, str] = Field(default_factory=dict)
    vehicle_labels: dict[str, str] = Field(default_factory=dict)


class Facility2(Strict):
    w: float
    h: float


class CorridorV(Strict):
    id: str
    width: float
    one_way: bool = False
    quads: list[list[list[float]]]    # segment başına 4 köşe [[x,y]x4]


class ObstacleV(Strict):
    x: float
    y: float
    w: float
    h: float
    kind: str = "wall"


class StationV(Strict):
    id: str
    name: str
    x: float
    y: float
    w: float
    h: float
    cx: float
    cy: float
    inp: list[float]                  # giriş noktası [x,y] (JS'te 'inp')
    out: list[float]                  # çıkış noktası [x,y]
    capacity: int = 1
    requires_operator: bool = False


class PathV(Strict):
    from_id: str
    to_id: str
    vehicle: str
    pts: list[list[float]]
    cum: list[float]                  # kümülatif yay uzunluğu (len == len(pts))
    len: float


class Scene(Strict):
    facility: Facility2
    corridors: list[CorridorV]
    obstacles: list[ObstacleV] = Field(default_factory=list)
    stations: list[StationV]
    paths: list[PathV] = Field(default_factory=list)   # yalnız logda kullanılanlar


class ProductV(Strict):
    id: str
    name: str
    color: str


class Entities(Strict):
    id: list[str] = Field(default_factory=list)
    product: list[int] = Field(default_factory=list)   # products indeksi
    batch: list[int] = Field(default_factory=list)
    t_created: list[float] = Field(default_factory=list)
    t_done: list[float] = Field(default_factory=list)  # -1 = bitmedi


class Segs(Strict):
    """Varlık durum aralıkları (t0'a göre sıralı, kararlı). ref: kind 0/1/2/3/4/6'da station indeksi, kind 5'te paths indeksi."""
    ent: list[int] = Field(default_factory=list)
    kind: list[int] = Field(default_factory=list)
    t0: list[float] = Field(default_factory=list)
    t1: list[float] = Field(default_factory=list)
    ref: list[int] = Field(default_factory=list)
    veh: list[int] = Field(default_factory=list)       # vehicles indeksi veya -1


class VehicleV(Strict):
    id: str
    type: str


class VSegs(Strict):
    """Araç aralıkları. ref: IDLE/WORK'te 'noktalar' için station indeksi, -1 = park tepsisi (konum modellenmedi); TRAVEL'da paths indeksi."""
    veh: list[int] = Field(default_factory=list)
    kind: list[int] = Field(default_factory=list)
    t0: list[float] = Field(default_factory=list)
    t1: list[float] = Field(default_factory=list)
    ref: list[int] = Field(default_factory=list)
    ent: list[int] = Field(default_factory=list)       # taşınan/işlenen varlık veya -1
    jump: list[int] = Field(default_factory=list)      # 1 = önceki bitiş noktasından farklı başladı (ışınlanma)


class ConsistencyItem(Strict):
    twin: float | None = None
    sim: float | None = None
    ok: bool | None = None            # None = karşılaştırılmadı
    note: str = ""


class TwinData(Strict):
    schema_version: int = SCHEMA_VERSION
    meta: TwinMeta
    scene: Scene
    products: list[ProductV]
    vehicles: list[VehicleV]
    entities: Entities
    segs: Segs
    vsegs: VSegs
    series: dict[str, list[list[float]]] = Field(default_factory=dict)   # ad -> [t[], v[]] adım fonksiyonu
    consistency: dict[str, ConsistencyItem] = Field(default_factory=dict)
