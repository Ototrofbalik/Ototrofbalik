"""Ortak temel tipler. Birim: metre (m), saniye (s). Orijin: tesis sol-alt köşe, +x sağ, +y yukarı."""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Point(Strict):
    x: float
    y: float

    def as_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)


class Rect(Strict):
    """Eksene hizalı dikdörtgen: sol-alt (x,y), genişlik w (x yönü), yükseklik h (y yönü)."""
    x: float
    y: float
    w: float = Field(gt=0)
    h: float = Field(gt=0)

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h

    def intersects(self, o: Rect, gap: float = 0.0) -> bool:
        """gap>0: aralarında en az gap metre boşluk yoksa True."""
        return not (self.x2 + gap <= o.x or o.x2 + gap <= self.x or self.y2 + gap <= o.y or o.y2 + gap <= self.y)

    def contains_point(self, p: Point) -> bool:
        return self.x <= p.x <= self.x2 and self.y <= p.y <= self.y2


class VehicleType(StrEnum):
    OPERATOR = "operator"   # yürüyen operatör
    FORKLIFT = "forklift"
    AGV = "agv"


class StationKind(StrEnum):
    RAW_MATERIAL = "raw_material"
    MACHINE = "machine"
    ASSEMBLY = "assembly"
    QUALITY = "quality"
    PACKAGING = "packaging"
    SHIPPING = "shipping"
    WAREHOUSE = "warehouse"
    OPERATOR_AREA = "operator_area"
