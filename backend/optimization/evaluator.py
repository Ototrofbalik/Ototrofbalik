"""[Optimization Engineer] Tam (exact) değerlendirme + önbellek, ağırlıklı hedef ve vekil hedef.

Tam değerlendirme = `validate_layout` + `compute_routes` (network) + `compute_flow_metrics`. Erişilemeyen akış
(UnreachableFlowError) veya herhangi bir validasyon hatası adayı uygulanamaz (infeasible) yapar.

Hedef (normalize, boyutsuz): Σ w_k · c_k / c_k(baseline)
  c_distance  = D_total (m/sa, ağ)               c_walking = operatör yürüyüşü (m/sa, ağ)
  c_adjacency = Σ yakınlık çiftleri merkez-merkez Öklid mesafesi (m)
  c_area      = istasyonları saran dikdörtgen alanı (m²)
Baseline bileşeni 0 ise normalizasyon paydası 1 alınır. Baseline hedefi bu nedenle Σ w_k (bileşen>0 olanlar).
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from backend.layout.validation import validate_layout
from backend.optimization.placement import Placement, PlacementSpace, center_of, footprint, state_key
from backend.optimization.surrogate import CorridorNet
from backend.routing.engine import UnreachableFlowError, compute_flow_metrics, compute_routes
from backend.schemas import FlowEdge, FlowMetrics, Layout, ObjectiveWeights, Project, VehicleType

INF = math.inf


@dataclass(frozen=True)
class Components:
    distance: float
    walking: float
    adjacency: float
    area: float


@dataclass(frozen=True)
class ExactEval:
    feasible: bool
    comps: Components | None
    reason: str = ""


class Objective:
    def __init__(self, weights: ObjectiveWeights, base: Components) -> None:
        self.w = weights
        self.base = base

    @staticmethod
    def _n(v: float, b: float) -> float:
        return v / b if b > 1e-12 else v

    def __call__(self, c: Components | None) -> float:
        if c is None:
            return INF
        b, w = self.base, self.w
        return (w.distance * self._n(c.distance, b.distance) + w.walking * self._n(c.walking, b.walking)
                + w.adjacency * self._n(c.adjacency, b.adjacency) + w.area * self._n(c.area, b.area))


def bbox_area(rects) -> float:
    rects = list(rects)
    if not rects:
        return 0.0
    return (max(r.x2 for r in rects) - min(r.x for r in rects)) * (max(r.y2 for r in rects) - min(r.y for r in rects))


def layout_components(layout: Layout, flow: FlowMetrics, adjacency_pairs: list[tuple[str, str]]) -> Components:
    ids = {s.id for s in layout.workstations}
    adj = 0.0
    for a, b in adjacency_pairs:
        if a in ids and b in ids:
            ca, cb = layout.station(a).center(), layout.station(b).center()
            adj += math.hypot(ca.x - cb.x, ca.y - cb.y)
    return Components(distance=flow.total_distance_m_per_hour, walking=flow.operator_walking_m_per_hour,
                      adjacency=adj, area=bbox_area(s.footprint() for s in layout.workstations))


class Evaluator:
    """Tam değerlendirme önbelleği (durum anahtarı -> ExactEval) ve vekil hesaplayıcı."""

    def __init__(self, project: Project, edges: list[FlowEdge], space: PlacementSpace,
                 adjacency_pairs: list[tuple[str, str]]) -> None:
        self.project = project
        self.edges = edges
        self.space = space
        self.adjacency_pairs = [(a, b) for a, b in adjacency_pairs]
        self.net = CorridorNet(space.layout)
        self._cache: dict = {}
        self.n_exact = 0
        self.reject_reasons: Counter[str] = Counter()

    # ------------------------------------------------------------------ tam değerlendirme
    def exact(self, state: dict[str, Placement]) -> ExactEval:
        k = state_key(state)
        hit = self._cache.get(k)
        if hit is not None:
            return hit
        self.n_exact += 1
        lay = self.space.build_layout(state)
        rep = validate_layout(lay)
        if not rep.valid:
            codes = sorted({i.code for i in rep.issues})
            for c in codes:
                self.reject_reasons[c] += 1
            res = ExactEval(False, None, ",".join(codes))
        else:
            routes = compute_routes(lay, self.edges)
            try:
                flow = compute_flow_metrics(lay, self.edges, routes, self.project)
            except UnreachableFlowError as e:
                self.reject_reasons["UNREACHABLE_FLOW"] += 1
                res = ExactEval(False, None, f"UNREACHABLE_FLOW: {e}")
            else:
                res = ExactEval(True, layout_components(lay, flow, self.adjacency_pairs))
        self._cache[k] = res
        return res

    # ------------------------------------------------------------------ vekil
    def edge_dist(self, state: dict[str, Placement], e: FlowEdge) -> float:
        if e.from_id == e.to_id:
            return 0.0
        a = self._info(state, e.from_id)
        b = self._info(state, e.to_id)
        if not a.ok or not b.ok:
            return INF
        return self.net.stub_route(a.out_stubs, b.in_stubs, e.vehicle)

    def _info(self, state: dict[str, Placement], sid: str):
        if sid in state:
            return self.space.info(sid, state[sid])
        return self.space.fixed_info(sid)

    def surrogate(self, state: dict[str, Placement]) -> Components | None:
        d = w = 0.0
        for e in self.edges:
            de = self.edge_dist(state, e)
            if de == INF:
                return None
            d += e.frequency_per_hour * de
            if e.vehicle == VehicleType.OPERATOR:
                w += e.frequency_per_hour * de
        return Components(distance=d, walking=w, adjacency=self._adj(state), area=self._area(state))

    def _center(self, state: dict[str, Placement], sid: str) -> tuple[float, float]:
        ws = self.space.ws[sid]
        if sid in state:
            return center_of(ws, state[sid])
        c = ws.center()
        return (c.x, c.y)

    def _adj(self, state: dict[str, Placement]) -> float:
        tot = 0.0
        for a, b in self.adjacency_pairs:
            if a in self.space.ws and b in self.space.ws:
                ca, cb = self._center(state, a), self._center(state, b)
                tot += math.hypot(ca[0] - cb[0], ca[1] - cb[1])
        return tot

    def _area(self, state: dict[str, Placement]) -> float:
        fps = [footprint(s, state[s.id]) if s.id in state else s.footprint() for s in self.space.layout.workstations]
        return bbox_area(fps)
