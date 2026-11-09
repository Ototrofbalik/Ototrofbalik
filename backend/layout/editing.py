"""[Layout&Routing Engineer] Layout değiştirme (saf fonksiyonlar; yeni Layout döner, girdiyi mutasyona uğratmaz)."""
from backend.schemas import Layout, Workstation


def move_workstation(layout: Layout, station_id: str, x: float, y: float, rotation: str | None = None) -> Layout:
    raise NotImplementedError


def add_workstation(layout: Layout, ws: Workstation) -> Layout:
    raise NotImplementedError


def remove_workstation(layout: Layout, station_id: str) -> Layout:
    raise NotImplementedError


def clone_layout(layout: Layout, new_id: str, name: str) -> Layout:
    """parent_id = layout.id, version+1."""
    raise NotImplementedError
