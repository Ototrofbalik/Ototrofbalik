"""Ortak geometri yardımcıları (layout doğrulama ve rotalama aynı mantığı kullanır).

Döndürme: rotation derece olarak saat yönünün tersine (CCW) merkez etrafında uygulanır;
(dx, dy) -> 90: (-dy, dx), 180: (-dx, -dy), 270: (dy, -dx).
"""
from __future__ import annotations

import math

from backend.schemas import Layout, Rect, Rotation, Workstation

EPS = 1e-9
XY = tuple[float, float]


def rotate_offset(dx: float, dy: float, rotation: Rotation) -> XY:
    if rotation == Rotation.R90:
        return (-dy, dx)
    if rotation == Rotation.R180:
        return (-dx, -dy)
    if rotation == Rotation.R270:
        return (dy, -dx)
    return (dx, dy)


def terminal_point(ws: Workstation, kind: str) -> XY:
    """kind: 'in' | 'out'. Döndürme uygulanmış mutlak giriş/çıkış noktası."""
    off = ws.input_offset if kind == "in" else ws.output_offset
    c = ws.center()
    rx, ry = rotate_offset(off.x, off.y, ws.rotation)
    return (c.x + rx, c.y + ry)


def dist(a: XY, b: XY) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def polyline_length(pts: list[XY]) -> float:
    return sum(dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def closest_on_segment(p: XY, a: XY, b: XY) -> XY:
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    if l2 < EPS:
        return a
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
    return (a[0] + t * dx, a[1] + t * dy)


def point_rect_distance(p: XY, r: Rect) -> float:
    dx = max(r.x - p[0], 0.0, p[0] - r.x2)
    dy = max(r.y - p[1], 0.0, p[1] - r.y2)
    return math.hypot(dx, dy)


def rect_rect_distance(a: Rect, b: Rect) -> float:
    dx = max(b.x - a.x2, 0.0, a.x - b.x2)
    dy = max(b.y - a.y2, 0.0, a.y - b.y2)
    return math.hypot(dx, dy)


def rects_overlap_strict(a: Rect, b: Rect, tol: float = 1e-7) -> bool:
    return not (a.x2 <= b.x + tol or b.x2 <= a.x + tol or a.y2 <= b.y + tol or b.y2 <= a.y + tol)


def segment_interior_overlap(a: XY, b: XY, r: Rect, tol: float = 1e-7) -> bool:
    """Doğru parçası dikdörtgenin İÇİNDEN (pozitif uzunlukta) geçiyor mu? Yalnız kenara değme False."""
    x0, y0 = a
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - r.x), (dx, r.x2 - x0), (-dy, y0 - r.y), (dy, r.y2 - y0)):
        if abs(p) < EPS:
            if q <= tol:        # paralel ve kenar üstünde/dışında
                return False
        else:
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 >= t1:
                return False
    length = math.hypot(dx, dy)
    return (t1 - t0) * length > tol


def segment_rect_blocked(a: XY, b: XY, r: Rect, half_width: float = 0.0) -> bool:
    """Eksene hizalı segment için yarı genişlik kadar şişirilmiş şerit, aksi halde merkez çizgisi."""
    if half_width > 0 and (abs(a[0] - b[0]) < EPS or abs(a[1] - b[1]) < EPS):
        x1, x2 = sorted((a[0], b[0]))
        y1, y2 = sorted((a[1], b[1]))
        if abs(a[0] - b[0]) < EPS:
            band = Rect(x=x1 - half_width, y=y1, w=2 * half_width, h=max(y2 - y1, 1e-6))
        else:
            band = Rect(x=x1, y=y1 - half_width, w=max(x2 - x1, 1e-6), h=2 * half_width)
        return rects_overlap_strict(band, r)
    return segment_interior_overlap(a, b, r)


def segment_intersection(a: XY, b: XY, c: XY, d: XY) -> XY | None:
    """Paralel olmayan iki segmentin kesişim noktası; yoksa None."""
    r = (b[0] - a[0], b[1] - a[1])
    s = (d[0] - c[0], d[1] - c[1])
    den = r[0] * s[1] - r[1] * s[0]
    if abs(den) < EPS:
        return None
    qp = (c[0] - a[0], c[1] - a[1])
    t = (qp[0] * s[1] - qp[1] * s[0]) / den
    u = (qp[0] * r[1] - qp[1] * r[0]) / den
    if -1e-9 <= t <= 1 + 1e-9 and -1e-9 <= u <= 1 + 1e-9:
        return (a[0] + t * r[0], a[1] + t * r[1])
    return None


def on_segment(p: XY, a: XY, b: XY, tol: float = 1e-7) -> bool:
    return dist(p, closest_on_segment(p, a, b)) <= tol


def key(p: XY) -> XY:
    """Düğüm anahtarı: yuvarlanmış koordinat."""
    return (round(p[0], 6), round(p[1], 6))


def corridor_segments(layout: Layout) -> list[tuple[str, XY, XY]]:
    out = []
    for c in layout.corridors:
        for i in range(len(c.points) - 1):
            out.append((c.id, c.points[i].as_tuple(), c.points[i + 1].as_tuple()))
    return out


def station_stub_candidates(layout: Layout, ws: Workstation, term: XY, max_stubs: int = 3) -> list[list[XY]]:
    """Terminal noktasından koridora giden geçerli (engel/başka istasyon içinden geçmeyen) kısa bağlantılar.

    Her aday polyline [term, ..., attach]. Erişim mesafesi: attach noktasının istasyon ayak izine
    uzaklığı <= facility.station_access_max_m. Düz ve L biçimli aday denenir; en kısa `max_stubs` döner.
    """
    fp = ws.footprint()
    limit = layout.facility.station_access_max_m
    blockers = [o.rect for o in layout.obstacles] + [s.footprint() for s in layout.workstations if s.id != ws.id]

    attach: list[XY] = []
    for _, a, b in corridor_segments(layout):
        attach.extend([closest_on_segment(term, a, b), a, b])
        for axis_pt in ((term[0], a[1]), (a[0], term[1])):
            if on_segment(axis_pt, a, b):
                attach.append(axis_pt)
    seen: set[XY] = set()
    cands: list[list[XY]] = []
    for at in attach:
        k = key(at)
        if k in seen:
            continue
        seen.add(k)
        if point_rect_distance(at, fp) > limit + 1e-9:
            continue
        routes = [[term, at]]
        if abs(term[0] - at[0]) > EPS and abs(term[1] - at[1]) > EPS:
            routes.append([term, (term[0], at[1]), at])
            routes.append([term, (at[0], term[1]), at])
        for pl in routes:
            if any(segment_interior_overlap(pl[i], pl[i + 1], r) for i in range(len(pl) - 1) for r in blockers):
                continue
            cands.append(pl)
    cands.sort(key=polyline_length)
    uniq: list[list[XY]] = []
    used: set[XY] = set()
    for pl in cands:
        if key(pl[-1]) in used:
            continue
        used.add(key(pl[-1]))
        uniq.append(pl)
        if len(uniq) >= max_stubs:
            break
    return uniq
