"""[Optimization Engineer] Yerleşim durumu, aday konumlar ve hızlı geometrik ön-eleme.

Durum (State): taşınabilir her istasyon için (x, y, rotation) — sabit istasyonlar durumda yer almaz, asla
taşınmaz. Hızlı ön-eleme yalnız `validate_layout` ile AYNI kuralların ucuz bir alt kümesidir (sınır, engel,
sabit istasyon, güvenlik boşluğu, koridor blokajı, tek başına koridor erişimi); nihai karar her zaman
`validate_layout`'tadır.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from backend.layout.geometry import (
    XY,
    corridor_segments,
    rect_rect_distance,
    rects_overlap_strict,
    segment_rect_blocked,
    station_stub_candidates,
    terminal_point,
)
from backend.schemas import Layout, Rect, Rotation, Workstation

ROTATIONS: tuple[Rotation, ...] = (Rotation.R0, Rotation.R90, Rotation.R180, Rotation.R270)
MAX_GRID_POSITIONS = 1500   # rotasyon başına ızgara aday üst sınırı (aşılırsa adım büyütülür)

Placement = tuple[float, float, Rotation]          # (x, y, rotation) — sol-alt köşe
State = tuple[tuple[str, float, float, str], ...]  # sıralı, hash'lenebilir durum anahtarı


def r6(v: float) -> float:
    return round(v + 0.0, 6)


def dims(ws: Workstation, rot: Rotation) -> tuple[float, float]:
    """Döndürülmüş ayak izi (w, h)."""
    if rot in (Rotation.R90, Rotation.R270):
        return ws.length, ws.width
    return ws.width, ws.length


def placement_from_center(ws: Workstation, cx: float, cy: float, rot: Rotation) -> Placement:
    w, h = dims(ws, rot)
    return (r6(cx - w / 2), r6(cy - h / 2), rot)


def center_of(ws: Workstation, p: Placement) -> XY:
    w, h = dims(ws, p[2])
    return (p[0] + w / 2, p[1] + h / 2)


def footprint(ws: Workstation, p: Placement) -> Rect:
    w, h = dims(ws, p[2])
    return Rect(x=p[0], y=p[1], w=w, h=h)


def placed(ws: Workstation, p: Placement) -> Workstation:
    return ws.model_copy(update={"x": p[0], "y": p[1], "rotation": p[2]})


def state_key(state: dict[str, Placement]) -> State:
    return tuple((sid, r6(p[0]), r6(p[1]), p[2].value) for sid, p in sorted(state.items()))


def pair_conflict(a: Rect, b: Rect, gap: float) -> bool:
    """validate_layout ile aynı: çakışma veya güvenlik boşluğu ihlali."""
    if rects_overlap_strict(a, b, tol=1e-9):
        return True
    return rect_rect_distance(a, b) < gap - 1e-9


@dataclass
class StandaloneInfo:
    """Bir istasyonun belirli bir konumda, yalnız sabit varlıklarla birlikte değerlendirilmesi."""
    ok: bool
    fp: Rect
    in_stubs: list[list[XY]] = field(default_factory=list)
    out_stubs: list[list[XY]] = field(default_factory=list)


class PlacementSpace:
    """Aday konum üretimi + tek-başına uygulanabilirlik önbelleği."""

    def __init__(self, layout: Layout, movable_ids: list[str]) -> None:
        self.layout = layout
        self.fac = layout.facility
        self.movable_ids = movable_ids
        self.ws = {s.id: s for s in layout.workstations}
        self.fixed = [s for s in layout.workstations if s.id not in set(movable_ids)]
        self._fixed_fps = [s.footprint() for s in self.fixed]
        self._segs = corridor_segments(layout)
        self._widths = {c.id: c.width for c in layout.corridors}
        self._cache: dict[tuple, StandaloneInfo] = {}
        self.candidates: dict[str, list[Placement]] = {}

    # ------------------------------------------------------------------ tek-başına kontrol
    @staticmethod
    def _shape(ws: Workstation) -> tuple:
        return (ws.width, ws.length, ws.input_offset.x, ws.input_offset.y, ws.output_offset.x, ws.output_offset.y)

    def info(self, sid: str, p: Placement) -> StandaloneInfo:
        ws = self.ws[sid]
        k = (self._shape(ws), r6(p[0]), r6(p[1]), p[2])
        hit = self._cache.get(k)
        if hit is not None:
            return hit
        res = self._compute(ws, p)
        self._cache[k] = res
        return res

    def fixed_info(self, sid: str) -> StandaloneInfo:
        """Sabit istasyonun stub'ları (yalnız sabit varlıklar engel)."""
        k = ("fixed", sid)
        if k not in self._cache:
            ws = self.ws[sid]
            probe = self.layout.model_copy(update={"workstations": list(self.fixed)})
            ins = station_stub_candidates(probe, ws, terminal_point(ws, "in"))
            outs = station_stub_candidates(probe, ws, terminal_point(ws, "out"))
            self._cache[k] = StandaloneInfo(ok=bool(ins and outs), fp=ws.footprint(), in_stubs=ins, out_stubs=outs)
        return self._cache[k]

    def _compute(self, ws: Workstation, p: Placement) -> StandaloneInfo:
        fp = footprint(ws, p)
        bad = StandaloneInfo(ok=False, fp=fp)
        if fp.x < -1e-9 or fp.y < -1e-9 or fp.x2 > self.fac.width + 1e-9 or fp.y2 > self.fac.height + 1e-9:
            return bad
        if any(rects_overlap_strict(fp, o.rect, tol=1e-9) for o in self.layout.obstacles):
            return bad
        if any(pair_conflict(fp, f, self.fac.safety_gap_m) for f in self._fixed_fps):
            return bad
        if any(segment_rect_blocked(a, b, fp, self._widths[cid] / 2) for cid, a, b in self._segs):
            return bad
        cand = placed(ws, p)
        probe = self.layout.model_copy(update={"workstations": [*self.fixed, cand]})
        ins = station_stub_candidates(probe, cand, terminal_point(cand, "in"))
        outs = station_stub_candidates(probe, cand, terminal_point(cand, "out"))
        if not ins or not outs:
            return bad
        return StandaloneInfo(ok=True, fp=fp, in_stubs=ins, out_stubs=outs)

    # ------------------------------------------------------------------ aday üretimi
    def build_candidates(self) -> None:
        """Izgara (facility.grid, gerekirse büyütülmüş adım) × 4 rotasyon + mevcut slotlar (merkez bazlı)."""
        step = max(self.fac.grid, 0.25)
        while (self.fac.width / step + 1) * (self.fac.height / step + 1) > MAX_GRID_POSITIONS:
            step *= 1.5
        self.grid_step = step
        slot_centers = [self.ws[s].center().as_tuple() for s in self.movable_ids]
        for sid in self.movable_ids:
            ws = self.ws[sid]
            seen: set[Placement] = set()
            out: list[Placement] = []
            for rot in ROTATIONS:
                w, h = dims(ws, rot)
                pts: list[Placement] = []
                nx_, ny_ = int((self.fac.width - w) / step + 1e-9), int((self.fac.height - h) / step + 1e-9)
                for i in range(nx_ + 1):
                    for j in range(ny_ + 1):
                        pts.append((r6(i * step), r6(j * step), rot))
                pts.extend(placement_from_center(ws, cx, cy, rot) for cx, cy in slot_centers)
                for p in pts:
                    if p in seen:
                        continue
                    seen.add(p)
                    if self.info(sid, p).ok:
                        out.append(p)
            out.sort(key=lambda q: (q[2].value, q[0], q[1]))
            self.candidates[sid] = out

    # ------------------------------------------------------------------ durum kontrolü
    def quick_ok(self, state: dict[str, Placement], changed: list[str]) -> bool:
        """Değişen istasyonlar için tek-başına uygunluk + diğer taşınabilir istasyonlarla çakışma/boşluk."""
        gap = self.fac.safety_gap_m
        for sid in changed:
            inf = self.info(sid, state[sid])
            if not inf.ok:
                return False
            for other, p in state.items():
                if other == sid:
                    continue
                if pair_conflict(inf.fp, footprint(self.ws[other], p), gap):
                    return False
        return True

    def build_layout(self, state: dict[str, Placement], layout_id: str | None = None) -> Layout:
        wss = [placed(s, state[s.id]) if s.id in state else s for s in self.layout.workstations]
        upd: dict = {"workstations": wss}
        if layout_id is not None:
            upd["id"] = layout_id
        return self.layout.model_copy(update=upd)
