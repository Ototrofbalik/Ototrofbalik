"""[Layout&Routing Engineer] Layout doğrulama."""
from __future__ import annotations

from backend.layout.geometry import (
    corridor_segments,
    point_rect_distance,
    rect_rect_distance,
    rects_overlap_strict,
    segment_rect_blocked,
    station_stub_candidates,
    terminal_point,
)
from backend.schemas import IssueSeverity, Layout, Rect, ValidationIssue, ValidationReport

_E = IssueSeverity.ERROR


def _issue(code: str, msg: str, ids: list[str]) -> ValidationIssue:
    return ValidationIssue(code=code, severity=_E, message=msg, entity_ids=ids)


def _outside(r: Rect, bounds: Rect) -> bool:
    return r.x < -1e-9 or r.y < -1e-9 or r.x2 > bounds.x2 + 1e-9 or r.y2 > bounds.y2 + 1e-9


def validate_layout(layout: Layout) -> ValidationReport:
    """Kontroller: sınırlar, istasyon çakışması, engel çakışması, güvenlik boşluğu (facility.safety_gap_m),
    koridor min genişliği, koridorların engelden geçmemesi, her istasyonun koridora erişimi
    (station_access_max_m). Hataları ValidationIssue olarak döner; error yoksa valid=True."""
    fac = layout.facility
    bounds = Rect(x=0, y=0, w=fac.width, h=fac.height)
    issues: list[ValidationIssue] = []
    ws_list = layout.workstations

    # sınırlar
    for ws in ws_list:
        if _outside(ws.footprint(), bounds):
            issues.append(_issue("OUT_OF_BOUNDS", f"İstasyon '{ws.name}' tesis sınırlarının dışında.", [ws.id]))
    for c in layout.corridors:
        if any(p.x < -1e-9 or p.y < -1e-9 or p.x > bounds.x2 + 1e-9 or p.y > bounds.y2 + 1e-9 for p in c.points):
            issues.append(_issue("OUT_OF_BOUNDS", f"Koridor '{c.id}' tesis sınırlarının dışına çıkıyor.", [c.id]))
    for o in layout.obstacles:
        if _outside(o.rect, bounds):
            issues.append(_issue("OUT_OF_BOUNDS", f"Engel '{o.name}' tesis sınırlarının dışında.", [o.id]))

    # istasyon-istasyon: çakışma / güvenlik boşluğu
    for i in range(len(ws_list)):
        for j in range(i + 1, len(ws_list)):
            a, b = ws_list[i], ws_list[j]
            ra, rb = a.footprint(), b.footprint()
            if rects_overlap_strict(ra, rb, tol=1e-9):
                issues.append(_issue("OVERLAP", f"'{a.name}' ile '{b.name}' çakışıyor.", [a.id, b.id]))
            else:
                d = rect_rect_distance(ra, rb)
                if d < fac.safety_gap_m - 1e-9:
                    issues.append(_issue(
                        "SAFETY_GAP",
                        f"'{a.name}' ile '{b.name}' arası {d:.2f} m < güvenlik boşluğu {fac.safety_gap_m:.2f} m.",
                        [a.id, b.id]))

    # istasyon-engel
    for ws in ws_list:
        for o in layout.obstacles:
            if rects_overlap_strict(ws.footprint(), o.rect, tol=1e-9):
                issues.append(_issue("OBSTACLE_OVERLAP", f"'{ws.name}' engel '{o.name}' ile çakışıyor.", [ws.id, o.id]))

    # koridorlar: genişlik, engel/istasyon blokajı
    widths = {c.id: c.width for c in layout.corridors}
    for c in layout.corridors:
        if c.width < fac.min_corridor_width_m - 1e-9:
            issues.append(_issue(
                "CORRIDOR_TOO_NARROW",
                f"Koridor '{c.id}' genişliği {c.width:.2f} m < minimum {fac.min_corridor_width_m:.2f} m.", [c.id]))
    for cid, a, b in corridor_segments(layout):
        for o in layout.obstacles:
            if segment_rect_blocked(a, b, o.rect, widths[cid] / 2):
                _add_unique(issues, _issue(
                    "CORRIDOR_BLOCKED", f"Koridor '{cid}' engel '{o.name}' tarafından kesiliyor.", [cid, o.id]))
        for ws in ws_list:
            if segment_rect_blocked(a, b, ws.footprint(), widths[cid] / 2):
                _add_unique(issues, _issue(
                    "CORRIDOR_BLOCKED", f"Koridor '{cid}' istasyon '{ws.name}' içinden geçiyor.", [cid, ws.id]))

    # istasyon-koridor erişimi (rotalamayla aynı mantık)
    for ws in ws_list:
        nearest = min(
            (point_rect_distance(p.as_tuple(), ws.footprint()) for c in layout.corridors for p in c.points),
            default=None)
        bad = [k for k in ("in", "out") if not station_stub_candidates(layout, ws, terminal_point(ws, k))]
        if bad:
            where = "giriş" if bad == ["in"] else "çıkış" if bad == ["out"] else "giriş/çıkış"
            near = f" (koridor köşe noktalarına en yakın ~{nearest:.1f} m)" if nearest is not None else ""
            issues.append(_issue(
                "NO_CORRIDOR_ACCESS",
                f"'{ws.name}' {where} noktası için en fazla {fac.station_access_max_m:.1f} m içinde "
                f"engelsiz koridor erişimi yok{near}.", [ws.id]))

    return ValidationReport(layout_id=layout.id, valid=not any(i.severity == _E for i in issues), issues=issues)


def _add_unique(issues: list[ValidationIssue], issue: ValidationIssue) -> None:
    if not any(i.code == issue.code and i.entity_ids == issue.entity_ids for i in issues):
        issues.append(issue)
