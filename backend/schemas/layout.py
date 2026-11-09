"""Layout sözleşmeleri."""
from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .common import Point, Rect, StationKind, Strict, VehicleType


class Rotation(StrEnum):
    R0 = "0"
    R90 = "90"
    R180 = "180"
    R270 = "270"


class Workstation(Strict):
    id: str
    name: str
    kind: StationKind = StationKind.MACHINE
    x: float                      # sol-alt köşe (döndürme uygulanmış ayak izinin sol-altı DEĞİL; bkz. footprint)
    y: float
    width: float = Field(gt=0)    # rotation=0 iken x yönü uzunluğu
    length: float = Field(gt=0)   # rotation=0 iken y yönü uzunluğu
    rotation: Rotation = Rotation.R0
    fixed: bool = False           # optimizer taşıyamaz
    # Giriş/çıkış noktaları: ayak izi merkezine göre ofset (rotation=0 koordinatında; döndürmeyle birlikte döner)
    input_offset: Point = Point(x=0.0, y=0.0)
    output_offset: Point = Point(x=0.0, y=0.0)
    # Süreç parametreleri
    processing_time_s: float = Field(default=60.0, ge=0)
    setup_time_s: float = Field(default=0.0, ge=0)
    capacity: int = Field(default=1, ge=1)         # paralel makine sayısı
    queue_capacity: int | None = None              # None = sınırsız
    requires_operator: bool = False

    def footprint(self) -> Rect:
        """Döndürme uygulanmış gerçek ayak izi (x,y = sol-alt köşe; 90/270 için w/h yer değiştirir)."""
        if self.rotation in (Rotation.R90, Rotation.R270):
            return Rect(x=self.x, y=self.y, w=self.length, h=self.width)
        return Rect(x=self.x, y=self.y, w=self.width, h=self.length)

    def center(self) -> Point:
        f = self.footprint()
        return Point(x=f.x + f.w / 2, y=f.y + f.h / 2)


class Corridor(Strict):
    """Genişlikli polyline koridor. Araç yolu = polyline noktaları."""
    id: str
    points: list[Point] = Field(min_length=2)
    width: float = Field(default=3.0, gt=0)
    one_way: bool = False                      # True: points sırasında tek yön
    allowed_vehicles: list[VehicleType] = Field(
        default_factory=lambda: [VehicleType.OPERATOR, VehicleType.FORKLIFT, VehicleType.AGV]
    )


class Obstacle(Strict):
    id: str
    name: str = "engel"
    rect: Rect
    kind: str = "wall"           # wall | column | restricted


class Facility(Strict):
    name: str = "Tesis"
    width: float = Field(gt=0)    # m (x)
    height: float = Field(gt=0)   # m (y)
    grid: float = 1.0
    safety_gap_m: float = 0.5               # istasyonlar arası min boşluk
    min_corridor_width_m: float = 2.0
    station_access_max_m: float = 6.0       # istasyon erişim noktasının koridora max mesafesi


class Layout(Strict):
    id: str
    name: str
    version: int = 1
    parent_id: str | None = None     # türetildiği layout (senaryo/sürüm zinciri)
    facility: Facility
    workstations: list[Workstation]
    corridors: list[Corridor]
    obstacles: list[Obstacle] = Field(default_factory=list)

    def station(self, sid: str) -> Workstation:
        for s in self.workstations:
            if s.id == sid:
                return s
        raise KeyError(sid)


class IssueSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class ValidationIssue(Strict):
    code: str     # OUT_OF_BOUNDS | OVERLAP | OBSTACLE_OVERLAP | SAFETY_GAP | NO_CORRIDOR_ACCESS | CORRIDOR_TOO_NARROW | UNKNOWN_STATION | CORRIDOR_BLOCKED
    severity: IssueSeverity
    message: str
    entity_ids: list[str] = Field(default_factory=list)


class ValidationReport(Strict):
    layout_id: str
    valid: bool                                  # error yoksa True
    issues: list[ValidationIssue] = Field(default_factory=list)
