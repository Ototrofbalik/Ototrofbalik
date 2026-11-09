"""[Layout&Routing Engineer] Layout değiştirme (saf fonksiyonlar; yeni Layout döner, girdiyi mutasyona uğratmaz)."""
from backend.schemas import Layout, Rotation, Workstation


def move_workstation(layout: Layout, station_id: str, x: float, y: float, rotation: str | None = None) -> Layout:
    """İstasyonu (x, y) sol-alt köşesine taşır; rotation verilirse ("0"|"90"|"180"|"270") döndürür.
    fixed istasyon taşınamaz (ValueError). Bilinmeyen id: KeyError."""
    ws = layout.station(station_id)
    if ws.fixed:
        raise ValueError(f"İstasyon '{station_id}' sabit (fixed); taşınamaz.")
    update: dict = {"x": x, "y": y}
    if rotation is not None:
        update["rotation"] = Rotation(str(rotation))
    out = layout.model_copy(deep=True)
    out.workstations = [
        s.model_copy(update=update) if s.id == station_id else s for s in out.workstations
    ]
    return out


def add_workstation(layout: Layout, ws: Workstation) -> Layout:
    if any(s.id == ws.id for s in layout.workstations):
        raise ValueError(f"İstasyon id '{ws.id}' zaten var.")
    out = layout.model_copy(deep=True)
    out.workstations.append(ws.model_copy(deep=True))
    return out


def remove_workstation(layout: Layout, station_id: str) -> Layout:
    layout.station(station_id)  # yoksa KeyError
    out = layout.model_copy(deep=True)
    out.workstations = [s for s in out.workstations if s.id != station_id]
    return out


def clone_layout(layout: Layout, new_id: str, name: str) -> Layout:
    """parent_id = layout.id, version+1."""
    return layout.model_copy(
        update={"id": new_id, "name": name, "parent_id": layout.id, "version": layout.version + 1}, deep=True)
