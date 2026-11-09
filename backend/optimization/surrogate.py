"""[Optimization Engineer] Hızlı ağ-mesafe vekili (surrogate).

Rotalama motoru ile aynı koridor modelini kullanır (koridor parçaları, kesişim/T-kavşak düğümleri,
`allowed_vehicles`, `one_way`) ama koridor düğümleri arası en kısa yolları BİR KEZ hesaplar (APSP önbelleği).
İstasyon erişim stub'ları `PlacementSpace` önbelleğinden gelir (yalnız sabit varlıklar engel kabul edilir);
bu nedenle vekil, taşınabilir istasyonların birbirinin stub'ını kapatmasını görmez. Vekil yalnız aday
SIRALAMA ve OR-Tools maliyet matrisi içindir; her öneri tam `compute_routes` + `validate_layout` ile
yeniden hesaplanır.
"""
from __future__ import annotations

import math

import networkx as nx

from backend.layout.geometry import (
    XY,
    corridor_segments,
    dist,
    key,
    on_segment,
    polyline_length,
    segment_intersection,
)
from backend.schemas import Layout, VehicleType

INF = math.inf


class CorridorNet:
    def __init__(self, layout: Layout) -> None:
        self.cor = {c.id: c for c in layout.corridors}
        segs = corridor_segments(layout)
        self.pieces: list[tuple[str, XY, XY]] = []
        for i, (cid, a, b) in enumerate(segs):
            pts: list[XY] = [a, b]
            for j, (_, c, d) in enumerate(segs):
                if i == j:
                    continue
                x = segment_intersection(a, b, c, d)
                if x is not None:
                    pts.append(x)
                for e in (c, d):
                    if on_segment(e, a, b):
                        pts.append(e)
            ln = max((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2, 1e-18)
            uniq = {key(p): p for p in pts}
            ordered = sorted(uniq.values(),
                             key=lambda p: ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / ln)
            for p, q in zip(ordered, ordered[1:], strict=False):
                if dist(p, q) > 1e-9:
                    self.pieces.append((cid, key(p), key(q)))
        self._apsp: dict[VehicleType, dict] = {}
        self._loc: dict[XY, list[int]] = {}
        self._pair: dict[tuple, float] = {}

    def _graph_apsp(self, v: VehicleType) -> dict:
        if v not in self._apsp:
            g = nx.DiGraph()
            for cid, p, q in self.pieces:
                c = self.cor[cid]
                if v not in c.allowed_vehicles:
                    continue
                w = dist(p, q)
                g.add_edge(p, q, weight=w)
                if not c.one_way:
                    g.add_edge(q, p, weight=w)
            self._apsp[v] = {s: d for s, d in nx.all_pairs_dijkstra_path_length(g, weight="weight")}
        return self._apsp[v]

    def _locate(self, p: XY) -> list[int]:
        k = key(p)
        if k not in self._loc:
            self._loc[k] = [i for i, (_, a, b) in enumerate(self.pieces) if on_segment(p, a, b, tol=1e-6)]
        return self._loc[k]

    def point_dist(self, s: XY, t: XY, v: VehicleType) -> float:
        """Koridor üzerindeki iki nokta arasındaki ağ mesafesi (yön ve araç kısıtlı)."""
        ck = (key(s), key(t), v)
        if ck in self._pair:
            return self._pair[ck]
        apsp = self._graph_apsp(v)
        best = INF
        for i in self._locate(s):
            cid, a, b = self.pieces[i]
            ci = self.cor[cid]
            if v not in ci.allowed_vehicles:
                continue
            exits = [(b, dist(s, b))] + ([] if ci.one_way else [(a, dist(s, a))])
            for j in self._locate(t):
                cjd, c, d = self.pieces[j]
                cj = self.cor[cjd]
                if v not in cj.allowed_vehicles:
                    continue
                if i == j:
                    forward = dist(a, t) >= dist(a, s) - 1e-9
                    if not ci.one_way or forward:
                        best = min(best, dist(s, t))
                entries = [(c, dist(c, t))] + ([] if cj.one_way else [(d, dist(d, t))])
                for u, lu in exits:
                    du = apsp.get(u)
                    if du is None:
                        continue
                    for w, lw in entries:
                        gd = du.get(w)
                        if gd is not None:
                            best = min(best, lu + gd + lw)
        self._pair[ck] = best
        return best

    def stub_route(self, out_stubs: list[list[XY]], in_stubs: list[list[XY]], v: VehicleType) -> float:
        best = INF
        for so in out_stubs:
            lo = polyline_length(so)
            for si in in_stubs:
                d = lo + self.point_dist(so[-1], si[-1], v) + polyline_length(si)
                best = min(best, d)
        return best
