"""[Layout&Routing Engineer] Koridor ağı + A*/Dijkstra rotalama.

Ağ modeli (mode='network'):
  * Koridor polyline'ları segmentlere ayrılır; kesişim / T-kavşak / istasyon erişim noktalarında düğüm oluşur.
  * Her araç tipi için ayrı yönlü graf (DiGraph): `allowed_vehicles` ve `one_way` uygulanır.
  * İstasyon çıkış (from) / giriş (to) noktası, koridora kısa "stub" ile bağlanır; stub engelden ve başka
    istasyonların ayak izinden geçmez ve `facility.station_access_max_m` ile sınırlıdır. Stub'lar yalnızca
    o istasyonun kendi terminali için sorguda eklenir (başka istasyondan geçiş kestirmesi oluşmaz).
  * Yol bulma: networkx A* (Öklid sezgisi, kabul edilebilir) — ağırlıklar metre.
"""
from __future__ import annotations

from collections import defaultdict

import networkx as nx

from backend.layout.geometry import (
    XY,
    corridor_segments,
    dist,
    key,
    on_segment,
    polyline_length,
    segment_intersection,
    station_stub_candidates,
    terminal_point,
)
from backend.schemas import (
    FlowEdge,
    FlowMetrics,
    FromToCell,
    Layout,
    Point,
    Project,
    Route,
    RouteSet,
    VehicleType,
)

# Project.sim_config.resources'ta aracın hızı yoksa (veya project verilmediyse) kullanılan VARSAYILAN hızlar.
# Bunlar kullanıcı verisi değil, belgelenmiş varsayımdır; sonuçta transport_time yalnız bu varsayıma dayanır.
DEFAULT_SPEED_MPS: dict[VehicleType, float] = {
    VehicleType.OPERATOR: 1.2, VehicleType.FORKLIFT: 2.5, VehicleType.AGV: 1.5,
}
_MODES = ("network", "euclidean", "manhattan")


class UnreachableFlowError(ValueError):
    """Akış kenarlarından biri için erişilebilir rota yok; D_total güvenilir hesaplanamaz."""


# --------------------------------------------------------------------------- flow edges
def derive_flow_edges(project: Project) -> list[FlowEdge]:
    """Ürün rotalarından From-To kenarları: frequency = arrival_rate / batch_size (adım geçişi başına).

    Aynı (from, to, vehicle) için farklı ürünlerin / tekrar geçişlerin frekansları toplanır;
    quantity_per_move frekans ağırlıklı ortalamadır. project.flow_edges doluysa aynen o kullanılır."""
    if project.flow_edges:
        return [e.model_copy(deep=True) for e in project.flow_edges]
    freq: dict[tuple[str, str, VehicleType], float] = defaultdict(float)
    qty_w: dict[tuple[str, str, VehicleType], float] = defaultdict(float)
    order: list[tuple[str, str, VehicleType]] = []
    for p in project.products:
        f = p.arrival_rate_per_hour / p.batch_size
        for prev, cur in zip(p.route, p.route[1:], strict=False):
            k = (prev.station_id, cur.station_id, cur.transport_in)
            if k not in freq:
                order.append(k)
            freq[k] += f
            qty_w[k] += f * p.batch_size
    return [
        FlowEdge(from_id=a, to_id=b, vehicle=v, frequency_per_hour=freq[(a, b, v)],
                 quantity_per_move=qty_w[(a, b, v)] / freq[(a, b, v)] if freq[(a, b, v)] else 1.0)
        for (a, b, v) in order
    ]


# --------------------------------------------------------------------------- network graph
class _Network:
    def __init__(self, layout: Layout) -> None:
        self.layout = layout
        self.terms: dict[tuple[str, str], list[list[XY]]] = {}
        for ws in layout.workstations:
            for kind in ("in", "out"):
                self.terms[(ws.id, kind)] = station_stub_candidates(layout, ws, terminal_point(ws, kind))
        self._graphs: dict[VehicleType, nx.DiGraph] = {}
        self._build_segments()

    def _build_segments(self) -> None:
        layout = self.layout
        segs = corridor_segments(layout)
        cor = {c.id: c for c in layout.corridors}
        attach_pts = [pl[-1] for lst in self.terms.values() for pl in lst]
        # her segment için ek bölme noktaları
        self.pieces: list[tuple[str, XY, XY]] = []   # (corridor_id, p, q) yönü polyline sırası
        for i, (cid, a, b) in enumerate(segs):
            pts: list[XY] = [a, b]
            for j, (_, c, d) in enumerate(segs):
                if i == j:
                    continue
                x = segment_intersection(a, b, c, d)
                if x is not None:
                    pts.append(x)
                for e in (c, d):          # T-kavşak / eş doğrusal örtüşme uçları
                    if on_segment(e, a, b):
                        pts.append(e)
            for ap in attach_pts:
                if on_segment(ap, a, b):
                    pts.append(ap)
            ln = max((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2, 1e-18)
            uniq = {key(p): p for p in pts}
            ordered = sorted(uniq.values(), key=lambda p: ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / ln)
            for p, q in zip(ordered, ordered[1:], strict=False):
                if dist(p, q) > 1e-9:
                    self.pieces.append((cid, key(p), key(q)))
        self.cor = cor

    def graph(self, vehicle: VehicleType) -> nx.DiGraph:
        if vehicle in self._graphs:
            return self._graphs[vehicle]
        g = nx.DiGraph()
        for cid, p, q in self.pieces:
            c = self.cor[cid]
            if vehicle not in c.allowed_vehicles:
                continue
            w = dist(p, q)
            if not g.has_edge(p, q):
                g.add_edge(p, q, weight=w, pts=[p, q])
            if not c.one_way and not g.has_edge(q, p):
                g.add_edge(q, p, weight=w, pts=[q, p])
        self._graphs[vehicle] = g
        return g

    def route(self, from_id: str, to_id: str, vehicle: VehicleType) -> list[XY] | None:
        g = self.graph(vehicle)
        src_t = self.terms[(from_id, "out")]
        dst_t = self.terms[(to_id, "in")]
        pos: dict = {n: n for n in g.nodes}
        s_pt = terminal_point(self.layout.station(from_id), "out")
        d_pt = terminal_point(self.layout.station(to_id), "in")
        pos["SRC"], pos["DST"] = s_pt, d_pt
        added: list = []
        try:
            for pl in src_t:
                at = key(pl[-1])
                if at in g:
                    g.add_edge("SRC", at, weight=polyline_length(pl), pts=list(pl))
                    added.append(("SRC", at))
            for pl in dst_t:
                at = key(pl[-1])
                if at in g:
                    rev = list(reversed(pl))
                    g.add_edge(at, "DST", weight=polyline_length(pl), pts=rev)
                    added.append((at, "DST"))
            if "SRC" not in g or "DST" not in g:
                return None
            try:
                path = nx.astar_path(g, "SRC", "DST", heuristic=lambda u, v: dist(pos[u], pos[v]), weight="weight")
            except nx.NetworkXNoPath:
                return None
            pts: list[XY] = []
            for u, v in zip(path, path[1:], strict=False):
                seg = g.edges[u, v]["pts"]
                pts.extend(seg if not pts else seg[1:])
            return pts
        finally:
            for u, v in added:
                if g.has_edge(u, v):
                    g.remove_edge(u, v)
            for n in ("SRC", "DST"):
                if n in g:
                    g.remove_node(n)


def _dedupe(pts: list[XY]) -> list[XY]:
    out: list[XY] = []
    for p in pts:
        if not out or dist(out[-1], p) > 1e-9:
            out.append(p)
    return out


def compute_routes(layout: Layout, edges: list[FlowEdge], mode: str = "network") -> RouteSet:
    """mode='network': koridor grafı (networkx) üzerinde Dijkstra/A*; engellerden geçmez; tek yön ve
    allowed_vehicles uyulur; operatör serbest zeminde yürüyebilir (koridor dışı erişim kısa stub'ları ile).
    mode='euclidean'|'manhattan': yalnız basit analiz; Route.mode bunu işaretler."""
    if mode not in _MODES:
        raise ValueError(f"Bilinmeyen rota modu '{mode}'; geçerli: {', '.join(_MODES)}")
    net = _Network(layout) if mode == "network" else None
    routes: list[Route] = []
    unreachable: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, VehicleType]] = set()
    for e in edges:
        k = (e.from_id, e.to_id, e.vehicle)
        if k in seen:
            continue
        seen.add(k)
        a, b = layout.station(e.from_id), layout.station(e.to_id)   # bilinmeyen id -> KeyError
        if e.from_id == e.to_id:
            p = terminal_point(a, "out")
            routes.append(Route(from_id=e.from_id, to_id=e.to_id, vehicle=e.vehicle,
                                points=[Point(x=p[0], y=p[1])], distance_m=0.0, mode=mode))
            continue
        if mode == "network":
            assert net is not None
            pts = net.route(e.from_id, e.to_id, e.vehicle)
            if pts is None:
                routes.append(Route(from_id=e.from_id, to_id=e.to_id, vehicle=e.vehicle, points=[],
                                    distance_m=0.0, reachable=False, mode=mode))
                unreachable.append((e.from_id, e.to_id, e.vehicle.value))
                continue
            pts = _dedupe(pts)
            d = polyline_length(pts)
        else:
            o, i = terminal_point(a, "out"), terminal_point(b, "in")
            if mode == "euclidean":
                pts, d = [o, i], dist(o, i)
            else:
                corner = (i[0], o[1])
                pts, d = _dedupe([o, corner, i]), abs(o[0] - i[0]) + abs(o[1] - i[1])
        routes.append(Route(from_id=e.from_id, to_id=e.to_id, vehicle=e.vehicle,
                            points=[Point(x=p[0], y=p[1]) for p in pts], distance_m=d, mode=mode))
    return RouteSet(layout_id=layout.id, mode=mode, routes=routes, unreachable=unreachable)


# --------------------------------------------------------------------------- metrics
def _speed(project: Project | None, vehicle: VehicleType) -> float:
    if project is not None:
        for r in project.sim_config.resources:
            if r.vehicle == vehicle:
                return r.speed_mps
    return DEFAULT_SPEED_MPS[vehicle]


def compute_flow_metrics(layout: Layout, edges: list[FlowEdge], routes: RouteSet, project: Project | None = None) -> FlowMetrics:
    """D_total = Σ freq_i × dist_i ; T = dist/speed(project.sim_config.resources).

    * Araç hızı project.sim_config.resources'tan; yoksa DEFAULT_SPEED_MPS varsayımı.
    * Maliyet yalnız project.cost varsa hesaplanır (aksi halde None).
    * Erişilemeyen / rotası olmayan bir kenar varsa UnreachableFlowError (sessizce eksik toplam üretilmez).
    * distance_per_unit_m: project varsa D_total / Σ ürün varış hızı; yoksa D_total / sistem giriş akışı
      (hiçbir kenarın hedefi olmayan istasyonlardan çıkan Σ freq × quantity)."""
    by_cell: dict[tuple[str, str], list[float]] = {}   # [moves, metres_per_hour]
    per_vehicle: dict[VehicleType, float] = defaultdict(float)
    time_s = 0.0
    cost = 0.0
    for e in edges:
        r = routes.get(e.from_id, e.to_id, e.vehicle)
        if r is None or not r.reachable:
            raise UnreachableFlowError(
                f"Akış {e.from_id}->{e.to_id} ({e.vehicle.value}) için erişilebilir rota yok.")
        mph = e.frequency_per_hour * r.distance_m
        per_vehicle[e.vehicle] += mph
        time_s += mph / _speed(project, e.vehicle)
        if project is not None and project.cost is not None:
            cost += mph * project.cost.cost_per_meter.get(e.vehicle, 0.0)
        c = by_cell.setdefault((e.from_id, e.to_id), [0.0, 0.0])
        c[0] += e.frequency_per_hour
        c[1] += mph
    cells = [
        FromToCell(from_id=a, to_id=b, moves_per_hour=m, distance_m=(mph / m if m > 0 else 0.0), metres_per_hour=mph)
        for (a, b), (m, mph) in by_cell.items()
    ]
    total = sum(per_vehicle.values())
    if project is not None:
        units = sum(p.arrival_rate_per_hour for p in project.products)
    else:
        dests = {e.to_id for e in edges}
        units = sum(e.frequency_per_hour * e.quantity_per_move for e in edges if e.from_id not in dests)
    return FlowMetrics(
        layout_id=layout.id, mode=routes.mode, total_distance_m_per_hour=total,
        distance_per_unit_m=(total / units if units > 0 else 0.0),
        operator_walking_m_per_hour=per_vehicle.get(VehicleType.OPERATOR, 0.0),
        forklift_m_per_hour=per_vehicle.get(VehicleType.FORKLIFT, 0.0),
        agv_m_per_hour=per_vehicle.get(VehicleType.AGV, 0.0),
        transport_time_s_per_hour=time_s, cells=cells,
        material_handling_cost_per_hour=(cost if project is not None and project.cost is not None else None),
    )
