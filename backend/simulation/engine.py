"""[SimPy Engineer] Gerçek SimPy ayrık olay simülasyonu.

Model özeti (ayrıntı: docs/simulation_methodology.md):
- Varlık = parti (lot). `Product.arrival_rate_per_hour` birim/saat; parti varış hızı = rate / batch_size.
- İstasyon kapasitesi `simpy.Resource(capacity)`; opsiyonel `queue_capacity` bir kabul (admission) kaynağıyla
  (capacity + queue_capacity) modellenir; sonraki istasyonda yer yoksa varlık mevcut istasyonu tutarak bekler (blokaj).
- Operatör havuzu `simpy.Store` (kimlikli); hem `requires_operator` işlemleri hem operatör taşımaları aynı havuzu kullanır.
- Taşıma süresi = mesafe / hız + yükleme + boşaltma; mesafe `RouteSet`'ten okunur, rota yoksa/ulaşılamazsa hata.
- CRN: her ürün için ayrı varış ve ayrı işlem-süresi `numpy.random.Generator` (SeedSequence([seed, akış, ürün])).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import sqrt

import numpy as np
import simpy
from scipy import stats as sp_stats

from backend.analytics.bottleneck import analyze_bottlenecks
from backend.schemas import (
    CI,
    EventRecord,
    EventType,
    Product,
    Project,
    ResourceConfig,
    ResourceStats,
    RouteSet,
    SimConfig,
    SimKPIs,
    SimResult,
    StationStats,
    VehicleType,
    Workstation,
)

MAX_EVENTS = 100_000   # replikasyon 0 olay logu üst sınırı


class SimulationError(ValueError):
    """Simülasyon girdisi çalıştırılamaz (eksik rota, kaynak, istasyon vb.)."""


class RouteUnavailableError(SimulationError):
    """Bir geçiş için RouteSet'te rota yok veya ulaşılamaz (reachable=False)."""


# --------------------------------------------------------------------------- yardımcılar
class _Level:
    """Parçalı-sabit bir seviyenin [w0, w1] penceresindeki zaman integrali."""

    def __init__(self, w0: float, w1: float) -> None:
        self.w0, self.w1 = w0, w1
        self.v = 0
        self.last = 0.0
        self.area = 0.0

    def _acc(self, t: float) -> None:
        a, b = max(self.last, self.w0), min(t, self.w1)
        if b > a:
            self.area += self.v * (b - a)
        self.last = t

    def change(self, t: float, d: int) -> None:
        self._acc(t)
        self.v += d

    def close(self) -> None:
        self._acc(self.w1)


def _overlap(a: float, b: float, w0: float, w1: float) -> float:
    return max(0.0, min(b, w1) - max(a, w0))


@dataclass
class _Entity:
    id: str
    product: Product
    created: float
    mults: list[float]
    queue_s: float = 0.0
    resource_wait_s: float = 0.0
    processing_s: float = 0.0
    setup_s: float = 0.0
    transport_s: float = 0.0
    loadunload_s: float = 0.0
    completed: float | None = None


@dataclass
class _Station:
    ws: Workstation
    res: simpy.Resource
    admit: simpy.Resource | None
    busy: _Level
    visit_waits: list[float] = field(default_factory=list)
    max_q: int = 0
    processed: int = 0
    proc_sum: float = 0.0
    setup_sum: float = 0.0


@dataclass
class _Pool:
    cfg: ResourceConfig
    store: simpy.Store
    busy: _Level
    distance_m: float = 0.0
    travel_s: float = 0.0


@dataclass
class _RepOut:
    kpis: SimKPIs
    stations: list[StationStats]
    resources: list[ResourceStats]
    events: list[EventRecord]
    notes: list[str]


# --------------------------------------------------------------------------- doğrulama
def _validate(project: Project, routes: RouteSet, cfg: SimConfig) -> dict[VehicleType, ResourceConfig]:
    layout = project.layout
    if cfg.warmup_s >= cfg.duration_s:
        raise SimulationError("warmup_s, duration_s değerinden küçük olmalıdır.")
    if cfg.arrival not in ("poisson", "deterministic"):
        raise SimulationError(f"Bilinmeyen varış modeli: {cfg.arrival!r}")
    if not (cfg.processing_variability == "deterministic" or cfg.processing_variability.startswith("triangular")):
        raise SimulationError(f"Bilinmeyen işlem süresi değişkenliği: {cfg.processing_variability!r}")
    if routes.layout_id != layout.id:
        raise SimulationError(
            f"RouteSet layout_id={routes.layout_id!r} ile proje layout_id={layout.id!r} uyuşmuyor (eski rota seti)."
        )
    if not project.products:
        raise SimulationError("Projede ürün yok.")
    res_by_v: dict[VehicleType, ResourceConfig] = {}
    for rc in cfg.resources:
        res_by_v[rc.vehicle] = rc
    ids = {w.id for w in layout.workstations}
    need_operator = False
    for p in project.products:
        for s in p.route:
            if s.station_id not in ids:
                raise SimulationError(f"Ürün {p.id!r}: bilinmeyen istasyon {s.station_id!r}.")
            if layout.station(s.station_id).requires_operator:
                need_operator = True
        for a, b in zip(p.route, p.route[1:], strict=False):
            if a.station_id == b.station_id:
                continue
            v = b.transport_in
            r = routes.get(a.station_id, b.station_id, v)
            if r is None:
                raise RouteUnavailableError(f"Rota yok: {a.station_id} -> {b.station_id} ({v.value}).")
            if not r.reachable:
                raise RouteUnavailableError(f"Rota ulaşılamaz: {a.station_id} -> {b.station_id} ({v.value}).")
            rc = res_by_v.get(v)
            if rc is None or rc.count < 1:
                raise SimulationError(f"{v.value} taşıma kaynağı tanımlı değil/adet 0 ({a.station_id} -> {b.station_id}).")
    if need_operator:
        rc = res_by_v.get(VehicleType.OPERATOR)
        if rc is None or rc.count < 1:
            raise SimulationError("requires_operator istasyonu var ama operatör kaynağı (count>=1) tanımlı değil.")
    return res_by_v


# --------------------------------------------------------------------------- bir replikasyon
class _Replication:
    def __init__(self, project: Project, routes: RouteSet, cfg: SimConfig, seed: int, record_events: bool):
        self.project, self.routes, self.cfg, self.seed = project, routes, cfg, seed
        self.w0, self.w1 = cfg.warmup_s, cfg.duration_s
        self.env = simpy.Environment()
        self.record = record_events
        self.events: list[EventRecord] = []
        self.events_truncated = False
        res_by_v = _validate(project, routes, cfg)
        self.stations: dict[str, _Station] = {}
        for w in project.layout.workstations:
            admit = None
            if w.queue_capacity is not None:
                admit = simpy.Resource(self.env, capacity=w.capacity + w.queue_capacity)
            self.stations[w.id] = _Station(w, simpy.Resource(self.env, capacity=w.capacity), admit,
                                           _Level(self.w0, self.w1))
        self.pools: dict[VehicleType, _Pool] = {}
        for v, rc in res_by_v.items():
            store = simpy.Store(self.env)
            for k in range(1, rc.count + 1):
                store.put(f"{v.value}-{k}")
            self.pools[v] = _Pool(rc, store, _Level(self.w0, self.w1))
        self.wip = _Level(self.w0, self.w1)
        self.wip_by_product: dict[str, int] = {p.id: 0 for p in project.products}
        self.done: list[_Entity] = []
        self.created = 0
        self.rejected: dict[str, int] = {p.id: 0 for p in project.products}
        self.triangular = cfg.processing_variability.startswith("triangular")

    # ---- olay logu
    def _log(self, type_: EventType, ent: _Entity, **kw) -> None:
        if not self.record:
            return
        if len(self.events) >= MAX_EVENTS:
            self.events_truncated = True
            return
        self.events.append(EventRecord(t=self.env.now, type=type_, entity_id=ent.id, product_id=ent.product.id, **kw))

    # ---- varışlar
    def _arrivals(self, idx: int, product: Product):
        arr_rng = np.random.default_rng(np.random.SeedSequence([self.seed, 0, idx]))
        proc_rng = np.random.default_rng(np.random.SeedSequence([self.seed, 1, idx]))
        lot_rate_s = product.arrival_rate_per_hour / product.batch_size / 3600.0
        mean_gap = 1.0 / lot_rate_s
        n = 0
        poisson = self.cfg.arrival == "poisson"
        while True:
            if poisson:
                yield self.env.timeout(float(arr_rng.exponential(mean_gap)))
            elif n > 0:
                yield self.env.timeout(mean_gap)
            n += 1
            # işlem süresi çarpanları varışta çekilir: akış, rota/yerleşimden bağımsız (CRN)
            if self.triangular:
                mults = [float(m) for m in proc_rng.triangular(0.8, 1.0, 1.2, size=len(product.route))]
            else:
                mults = [1.0] * len(product.route)
            if product.max_wip is not None and self.wip_by_product[product.id] >= product.max_wip:
                self.rejected[product.id] += 1
                continue
            ent = _Entity(f"{product.id}-{n:05d}", product, self.env.now, mults)
            self.created += 1
            self.wip.change(self.env.now, +1)
            self.wip_by_product[product.id] += 1
            self._log(EventType.ENTITY_CREATED, ent, station_id=product.route[0].station_id)
            self.env.process(self._flow(ent))

    # ---- varlık akışı
    def _flow(self, ent: _Entity):
        env = self.env
        steps = ent.product.route
        last = len(steps) - 1
        first = self.stations[steps[0].station_id]
        adm = None
        if first.admit is not None:
            t0 = env.now
            adm = first.admit.request()
            yield adm
            ent.queue_s += env.now - t0
        for i, step in enumerate(steps):
            st = self.stations[step.station_id]
            ws = st.ws
            arrive = env.now
            sreq = st.res.request()
            qlen = len(st.res.queue)
            if env.now >= self.w0:
                st.max_q = max(st.max_q, qlen)
            self._log(EventType.QUEUE_ENTERED, ent, station_id=ws.id, queue_len=qlen)
            yield sreq
            wait = env.now - arrive
            ent.queue_s += wait
            opid = None
            if ws.requires_operator:
                pool = self.pools[VehicleType.OPERATOR]
                t0 = env.now
                opid = yield pool.store.get()
                ent.resource_wait_s += env.now - t0
                pool.busy.change(env.now, +1)
            base = step.processing_time_s if step.processing_time_s is not None else ws.processing_time_s
            setup = step.setup_time_s if step.setup_time_s is not None else ws.setup_time_s
            proc = base * ent.mults[i]
            start = env.now
            if start >= self.w0:
                st.visit_waits.append(wait)
            self._log(EventType.PROCESS_STARTED, ent, station_id=ws.id, resource_id=opid)
            st.busy.change(start, +1)
            yield env.timeout(setup + proc)
            st.busy.change(env.now, -1)
            if opid is not None:
                pool.store.put(opid)
                pool.busy.change(env.now, -1)
            self._log(EventType.PROCESS_FINISHED, ent, station_id=ws.id, resource_id=opid)
            ent.processing_s += proc
            ent.setup_s += setup
            if self.w0 <= env.now <= self.w1:
                st.processed += 1
                st.proc_sum += proc
                st.setup_sum += setup
            nadm = None
            nxt = steps[i + 1] if i < last else None
            if nxt is not None:
                nst = self.stations[nxt.station_id]
                if nst.admit is not None and nst is not st:
                    t0 = env.now
                    nadm = nst.admit.request()
                    yield nadm          # blokaj: sonraki kuyrukta yer yok -> mevcut istasyon tutulur
                    ent.queue_s += env.now - t0
            st.res.release(sreq)
            if adm is not None:
                st.admit.release(adm)
            if nxt is None:
                break
            if nxt.station_id != ws.id:
                yield from self._transport(ent, ws.id, nxt.station_id, nxt.transport_in)
            adm = nadm
        ent.completed = env.now
        self.wip.change(env.now, -1)
        self.wip_by_product[ent.product.id] -= 1
        self._log(EventType.ENTITY_COMPLETED, ent, station_id=steps[-1].station_id)
        if self.w0 <= env.now <= self.w1:
            self.done.append(ent)

    def _transport(self, ent: _Entity, a: str, b: str, v: VehicleType):
        env = self.env
        pool = self.pools[v]
        route = self.routes.get(a, b, v)
        dist = route.distance_m
        rc = pool.cfg
        self._log(EventType.TRANSPORT_REQUESTED, ent, from_id=a, to_id=b, vehicle=v, distance_m=dist)
        t0 = env.now
        rid = yield pool.store.get()
        ent.resource_wait_s += env.now - t0
        pool.busy.change(env.now, +1)
        self._log(EventType.TRANSPORT_STARTED, ent, from_id=a, to_id=b, vehicle=v, resource_id=rid, distance_m=dist)
        travel = dist / rc.speed_mps
        t_start = env.now
        a_t, b_t = t_start + rc.load_time_s, t_start + rc.load_time_s + travel
        ov = _overlap(a_t, b_t, self.w0, self.w1)
        if travel > 0:
            pool.distance_m += dist * ov / travel
        pool.travel_s += ov
        yield env.timeout(rc.load_time_s + travel + rc.unload_time_s)
        pool.store.put(rid)
        pool.busy.change(env.now, -1)
        ent.transport_s += travel
        ent.loadunload_s += rc.load_time_s + rc.unload_time_s
        self._log(EventType.TRANSPORT_COMPLETED, ent, from_id=a, to_id=b, vehicle=v, resource_id=rid, distance_m=dist)

    # ---- çalıştır + KPI
    def run(self) -> _RepOut:
        for idx, p in enumerate(self.project.products):
            self.env.process(self._arrivals(idx, p))
        self.env.run(until=self.w1)
        for lv in [self.wip, *[s.busy for s in self.stations.values()], *[p.busy for p in self.pools.values()]]:
            lv.close()
        window = self.w1 - self.w0
        d = self.done
        n = len(d)

        def mean(f) -> float:
            return sum(f(e) for e in d) / n if n else 0.0

        units = sum(e.product.batch_size for e in d)
        all_waits = [w for s in self.stations.values() for w in s.visit_waits]
        kpis = SimKPIs(
            throughput_per_hour=units / (window / 3600.0),
            completed=units,
            avg_cycle_time_s=mean(lambda e: e.completed - e.created),
            avg_lead_time_s=mean(lambda e: e.completed - e.created),
            avg_wip=self.wip.area / window,
            avg_queue_wait_s=sum(all_waits) / len(all_waits) if all_waits else 0.0,
            max_queue_len=max((s.max_q for s in self.stations.values()), default=0),
            total_travel_distance_m=sum(p.distance_m for p in self.pools.values()),
            total_travel_time_s=sum(p.travel_s for p in self.pools.values()),
            avg_processing_s=mean(lambda e: e.processing_s),
            avg_setup_s=mean(lambda e: e.setup_s),
            avg_transport_s=mean(lambda e: e.transport_s),
            avg_loadunload_s=mean(lambda e: e.loadunload_s),
            avg_resource_wait_s=mean(lambda e: e.resource_wait_s),
            avg_queue_s=mean(lambda e: e.queue_s),
        )
        sts = []
        for s in self.stations.values():
            k = max(s.processed, 1)
            sts.append(StationStats(
                station_id=s.ws.id,
                utilization=s.busy.area / (s.ws.capacity * window),
                avg_queue_wait_s=sum(s.visit_waits) / len(s.visit_waits) if s.visit_waits else 0.0,
                max_queue_len=s.max_q,
                processed=s.processed,
                avg_process_s=s.proc_sum / k if s.processed else 0.0,
                avg_setup_s=s.setup_sum / k if s.processed else 0.0,
            ))
        rss = [
            ResourceStats(vehicle=v, utilization=p.busy.area / (p.cfg.count * window) if p.cfg.count else 0.0,
                          distance_m=p.distance_m, busy_s=p.busy.area, empty_travel_m=0.0)
            for v, p in self.pools.items()
        ]
        notes: list[str] = []
        if self.events_truncated:
            notes.append(f"Olay logu {MAX_EVENTS} kayıtta kesildi (replikasyon 0); KPI'lar tüm olaylardan hesaplandı.")
        for pid, c in self.rejected.items():
            if c:
                notes.append(f"Ürün {pid}: max_wip sınırı nedeniyle {c} varış reddedildi (kayıp).")
        notes.append(f"Replikasyon (seed={self.seed}): {self.created} parti oluşturuldu, "
                     f"{len(self.done)} parti pencerede tamamlandı.")
        return _RepOut(kpis, sts, rss, self.events, notes)


# --------------------------------------------------------------------------- toplama
_INT_FIELDS = {"completed", "max_queue_len"}
_KPI_UNITS = list(SimKPIs.model_fields)


def _aggregate_kpis(reps: list[SimKPIs]) -> SimKPIs:
    out = {}
    for f in _KPI_UNITS:
        vals = [getattr(r, f) for r in reps]
        if f == "max_queue_len":
            out[f] = max(vals)
        elif f == "completed":
            out[f] = round(sum(vals) / len(vals))
        else:
            out[f] = float(np.mean(vals))
    return SimKPIs(**out)


def _ci95(reps: list[SimKPIs]) -> dict[str, CI]:
    n = len(reps)
    if n < 2:
        return {}
    tcrit = float(sp_stats.t.ppf(0.975, n - 1))
    out: dict[str, CI] = {}
    for f in _KPI_UNITS:
        vals = np.array([float(getattr(r, f)) for r in reps])
        m = float(vals.mean())
        half = tcrit * float(vals.std(ddof=1)) / sqrt(n)
        out[f] = CI(mean=m, lo=m - half, hi=m + half, n=n)
    return out


def _aggregate_stations(reps: list[list[StationStats]]) -> list[StationStats]:
    out = []
    for i, first in enumerate(reps[0]):
        col = [r[i] for r in reps]
        out.append(StationStats(
            station_id=first.station_id,
            utilization=float(np.mean([c.utilization for c in col])),
            avg_queue_wait_s=float(np.mean([c.avg_queue_wait_s for c in col])),
            max_queue_len=max(c.max_queue_len for c in col),
            processed=round(float(np.mean([c.processed for c in col]))),
            avg_process_s=float(np.mean([c.avg_process_s for c in col])),
            avg_setup_s=float(np.mean([c.avg_setup_s for c in col])),
        ))
    return out


def _aggregate_resources(reps: list[list[ResourceStats]]) -> list[ResourceStats]:
    out = []
    for i, first in enumerate(reps[0]):
        col = [r[i] for r in reps]
        out.append(ResourceStats(
            vehicle=first.vehicle,
            utilization=float(np.mean([c.utilization for c in col])),
            distance_m=float(np.mean([c.distance_m for c in col])),
            busy_s=float(np.mean([c.busy_s for c in col])),
            empty_travel_m=0.0,
        ))
    return out


def run_simulation(project: Project, routes: RouteSet, config: SimConfig | None = None) -> SimResult:
    """Rota mesafeleri `routes` içinden (gerçek ağ rotası) gelir. Replikasyonlar seed+i ile; CRN: aynı seed
    iki senaryoda aynı varış/işlem süresi akışını üretmelidir (varış ve süre akışları ayrı RNG'ler).
    Kaynaklar simpy.Resource/Store; olay logu tüm EventType'ları içerir.

    Hata: rota yok/ulaşılamaz -> RouteUnavailableError; diğer geçersiz girdi -> SimulationError (ikisi de ValueError)."""
    cfg = config or project.sim_config
    reps = [_Replication(project, routes, cfg, cfg.seed + i, record_events=(i == 0)).run()
            for i in range(cfg.replications)]
    kp = [r.kpis for r in reps]
    kpis = _aggregate_kpis(kp)
    notes = list(reps[0].notes)
    if cfg.replications > 1:
        notes.append(f"{cfg.replications} replikasyon (seed={cfg.seed}..{cfg.seed + cfg.replications - 1}); "
                     "KPI'lar ortalamadır, max_queue_len replikasyonlar arası maksimumdur; olay logu yalnız replikasyon 0.")
    if cfg.warmup_s > 0:
        notes.append(f"Isınma (warmup) {cfg.warmup_s:.0f} s: istatistikler [{cfg.warmup_s:.0f}, {cfg.duration_s:.0f}] s penceresinde.")
    if routes.mode != "network":
        notes.append(f"Rota modu '{routes.mode}': mesafeler ağ rotası değil, yaklaşıktır.")
    if kpis.completed == 0:
        notes.append("Pencerede tamamlanan ürün yok; süre KPI'ları 0 gösterilir (veri yok anlamında).")
    notes.append("Lead time = cycle time (sipariş serbest bırakma/bitmiş ürün gecikmesi modellenmedi).")
    notes.append("Boşta/boş araç seyri modellenmedi (empty_travel_m = 0).")
    result = SimResult(
        layout_id=project.layout.id, config=cfg, seed=cfg.seed, kpis=kpis,
        stations=_aggregate_stations([r.stations for r in reps]),
        resources=_aggregate_resources([r.resources for r in reps]),
        events=reps[0].events,
        replication_kpis=kp if cfg.replications > 1 else [],
        ci95=_ci95(kp),
        notes=notes,
    )
    result.not_modelled = [*result.not_modelled, "öncelik/dağıtım kuralları (FIFO varsayıldı)"]
    result.bottleneck_station_id = analyze_bottlenecks(result)["bottleneck_station_id"]
    return result
