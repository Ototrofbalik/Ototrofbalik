"""[SimPy Engineer] SimResult olay logundan Digital Twin verisi (saf Python; DB/Streamlit bilmez).

Kurallar: docs/twin_spec.md. Olay SIRASI korunur (aynı t'de birden çok olay olabilir; t'ye göre yeniden sıralama
yok). Boş araç seyri UYDURULMAZ: araç görev sonunda bıraktığı yerde 'boşta' kalır, farklı yerden başlayan
görevde `jump=1` işaretlenir (ışınlanma). Tutarlılık hesapları yuvarlamadan ÖNCE yapılır.

Konum sözleşmesi (vsegs): LOAD/TRAVEL/UNLOAD'da ref = paths indeksi (LOAD: pts[0], UNLOAD: pts[-1]);
IDLE/WORK'te ref = istasyon indeksi (araç o istasyonun `inp` noktasında), -1 = park tepsisi (konum modellenmedi).
"""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass

from backend.layout.geometry import terminal_point
from backend.schemas import EventType, Layout, Product, RouteSet, SimResult, VehicleType
from backend.simulation import engine
from backend.spaghetti.chart import VEHICLE_COLOR, VEHICLE_LABEL

from .schema import (
    SEG_BLOCKED,
    SEG_LOAD,
    SEG_PROCESS,
    SEG_QUEUE,
    SEG_TRAVEL,
    SEG_UNLOAD,
    SEG_VEH_WAIT,
    VSEG_IDLE,
    VSEG_LOAD,
    VSEG_TRAVEL,
    VSEG_UNLOAD,
    VSEG_WORK,
    ConsistencyItem,
    CorridorV,
    Entities,
    Facility2,
    ObstacleV,
    PathV,
    ProductV,
    Scene,
    Segs,
    StationV,
    TwinData,
    TwinMeta,
    VehicleV,
    VSegs,
)

ROUND = 2   # t ve koordinat ondalık basamağı (testler tam doğrulama için artırabilir)
PRODUCT_PALETTE = ["#4e79a7", "#f28e2b", "#59a14f", "#b07aa1", "#edc948", "#76b7b2", "#9c755f", "#e15759"]
ET = EventType
_Pts = list[tuple[float, float]]


def _r(x: float) -> float:
    return round(x, ROUND) + 0.0


# --------------------------------------------------------------------------- yardımcılar
def _step_collapse(pts: _Pts) -> _Pts:
    """Aynı t'de son değer kalır; ardışık eşit değerler atılır. Başlangıç (0, 0) garanti."""
    out: _Pts = [(0.0, 0.0)]
    for t, v in pts:
        if t == out[-1][0]:
            out[-1] = (t, v)
        else:
            out.append((t, v))
    res: _Pts = []
    for t, v in out:
        if res and res[-1][1] == v:
            continue
        res.append((t, v))
    return res


def _from_deltas(deltas: _Pts) -> _Pts:
    cur, pts = 0.0, []
    for t, d in sorted(deltas, key=lambda x: x[0]):   # kararlı
        cur += d
        pts.append((t, cur))
    return _step_collapse(pts)


def _integral(pts: _Pts, a: float, b: float) -> float:
    tot = 0.0
    for i, (t, v) in enumerate(pts):
        nxt = pts[i + 1][0] if i + 1 < len(pts) else math.inf
        lo, hi = max(t, a), min(nxt, b)
        if hi > lo:
            tot += v * (hi - lo)
    return tot


def _final(pts: _Pts) -> float:
    return pts[-1][1] if pts else 0.0


def _ser(pts: _Pts) -> list[list[float]]:
    """Yuvarlanmış, aynı t'de son değer, tekrarsız adım serisi -> [t[], v[]]."""
    rp = _step_collapse([(_r(t), v) for t, v in pts])
    return [[t for t, _ in rp], [v for _, v in rp]]


def _corridor_quads(points: list[tuple[float, float]], width: float) -> list[list[list[float]]]:
    quads = []
    h = width / 2
    for (x1, y1), (x2, y2) in zip(points, points[1:], strict=False):
        dx, dy = x2 - x1, y2 - y1
        n = math.hypot(dx, dy)
        if n < 1e-12:
            continue
        nx, ny = -dy / n * h, dx / n * h
        quads.append([[_r(x1 + nx), _r(y1 + ny)], [_r(x2 + nx), _r(y2 + ny)],
                      [_r(x2 - nx), _r(y2 - ny)], [_r(x1 - nx), _r(y1 - ny)]])
    return quads


@dataclass
class _Task:
    """Bir aracın görevi: taşıma (LOAD+TRAVEL+UNLOAD) veya operatör işi (WORK)."""
    t0: float
    t1: float
    start_st: int
    end_st: int
    vsegs: list[tuple[int, float, float, int, int]]   # (vkind, t0, t1, ref, ent)


@dataclass
class _Ent:
    idx: int
    open: tuple[int, float, int] | None = None        # (kind, t0, ref)
    pend: tuple[float, int, int, int] | None = None   # TS: (ts, from_idx, to_idx, veh_idx)
    pend_route: int = -1
    pend_vtype: VehicleType | None = None
    pend_dist: float = 0.0
    work: tuple[float, int, int] | None = None        # (t0, veh, station)


def _check_inputs(layout: Layout, routes: RouteSet, sim: SimResult, window_s: float | None) -> None:
    if sim.layout_id != layout.id:
        raise ValueError(f"Simülasyon sonucu layout_id={sim.layout_id!r} ile yerleşim layout_id={layout.id!r} uyuşmuyor.")
    if routes.layout_id != layout.id:
        raise ValueError(f"RouteSet layout_id={routes.layout_id!r} ile yerleşim layout_id={layout.id!r} uyuşmuyor (eski rota seti).")
    if window_s is not None and window_s <= 0:
        raise ValueError("window_s pozitif olmalıdır.")
    seen: set[tuple[str, str, VehicleType]] = set()
    for e in sim.events:
        if e.type in (ET.TRANSPORT_REQUESTED, ET.TRANSPORT_STARTED, ET.TRANSPORT_COMPLETED):
            k = (e.from_id, e.to_id, e.vehicle)
            if k in seen:
                continue
            seen.add(k)
            r = routes.get(*k)
            vn = getattr(k[2], "value", k[2])
            if r is None:
                raise ValueError(f"Rota yok: {k[0]} -> {k[1]} ({vn}); olay logu bu rota setiyle çalıştırılmamış.")
            if not r.reachable:
                raise ValueError(f"Rota ulaşılamaz: {k[0]} -> {k[1]} ({vn}).")


# --------------------------------------------------------------------------- ana fonksiyon
def build_twin_data(layout: Layout, routes: RouteSet, sim: SimResult, *, scenario_label: str = "Mevcut",
                    window_s: float | None = None, products: list[Product] | None = None) -> TwinData:
    """Olay logundan TwinData. `products` (opsiyonel): ad ve batch_size için; verilmezse batch=1 varsayılır
    (batch_size>1 ise tutarlılık 'completed_units' kalemi ✗ çıkar -> çağıran `project.products` vermelidir)."""
    _check_inputs(layout, routes, sim, window_s)
    cfg = sim.config
    w0, dur = cfg.warmup_s, cfg.duration_s
    n_rep = cfg.replications
    notes: list[str] = []

    events = list(sim.events)
    truncated = len(events) >= engine.MAX_EVENTS or any(n.startswith("Olay logu") and "kesildi" in n for n in sim.notes)
    t_end = dur
    t_valid_end: float | None = None
    if truncated and events:
        t_last = events[-1].t
        v = math.floor(t_last * 100) / 100
        if v >= t_last - 1e-9:
            v -= 0.01
        t_valid_end = max(0.0, v)
        t_end = min(t_end, t_valid_end)
    cropped = False
    if window_s is not None and window_s < t_end:
        t_end = window_s
        cropped = True
    events = [e for e in events if e.t <= t_end]

    # ---- sahne
    st_idx = {w.id: i for i, w in enumerate(layout.workstations)}
    stations_v = []
    for w in layout.workstations:
        fp, c = w.footprint(), w.center()
        ip, op = terminal_point(w, "in"), terminal_point(w, "out")
        stations_v.append(StationV(
            id=w.id, name=w.name, x=_r(fp.x), y=_r(fp.y), w=_r(fp.w), h=_r(fp.h), cx=_r(c.x), cy=_r(c.y),
            inp=[_r(ip[0]), _r(ip[1])], out=[_r(op[0]), _r(op[1])], capacity=w.capacity,
            requires_operator=w.requires_operator))
    corridors_v = [CorridorV(id=c.id, width=c.width, one_way=c.one_way,
                             quads=_corridor_quads([p.as_tuple() for p in c.points], c.width)) for c in layout.corridors]
    obstacles_v = [ObstacleV(x=_r(o.rect.x), y=_r(o.rect.y), w=_r(o.rect.w), h=_r(o.rect.h), kind=o.kind)
                   for o in layout.obstacles]

    # ---- araçlar
    vehicles: list[VehicleV] = []
    veh_idx: dict[str, int] = {}
    cfg_by_type = {rc.vehicle: rc for rc in cfg.resources}
    for rc in cfg.resources:
        for k in range(1, rc.count + 1):
            vid = f"{rc.vehicle.value}-{k}"
            veh_idx[vid] = len(vehicles)
            vehicles.append(VehicleV(id=vid, type=rc.vehicle.value))
    vcolors: dict[str, str] = {}
    vlabels: dict[str, str] = {}
    for rc in cfg.resources:
        vcolors[rc.vehicle.value] = VEHICLE_COLOR[rc.vehicle]
        vlabels[rc.vehicle.value] = VEHICLE_LABEL[rc.vehicle]
    for v in vehicles:   # hem tip hem araç kimliği ile aranabilir
        vcolors[v.id] = vcolors[v.type]
        vlabels[v.id] = v.id

    # ---- ürünler
    prod_list: list[ProductV] = []
    prod_idx: dict[str, int] = {}
    batch_of: dict[str, int] = {}
    for p in products or []:
        prod_idx[p.id] = len(prod_list)
        prod_list.append(ProductV(id=p.id, name=p.name, color=PRODUCT_PALETTE[len(prod_list) % len(PRODUCT_PALETTE)]))
        batch_of[p.id] = p.batch_size
    if products is None:
        notes.append("Ürün tanımı verilmedi: parti büyüklüğü (batch_size) 1 varsayıldı.")

    # ---- olayları sırayla işle
    ents = Entities()
    ent_state: dict[str, _Ent] = {}
    paths: list[PathV] = []
    path_idx: dict[tuple[str, str, VehicleType], int] = {}
    segs: list[tuple[int, int, float, float, int, int]] = []   # ent, kind, t0, t1, ref, veh
    tasks: dict[int, list[_Task]] = {}
    scaled_count = 0
    max_q: dict[int, _Pts] = {}
    run_max: dict[int, int] = {}
    wip_deltas: _Pts = []
    comp_deltas: _Pts = []
    t_done: list[float] = []

    def path_of(a: str, b: str, v: VehicleType) -> int:
        k = (a, b, v)
        if k not in path_idx:
            r = routes.get(a, b, v)
            rp = [[_r(p.x), _r(p.y)] for p in r.points]
            cum = [0.0]
            for q in range(1, len(rp)):
                cum.append(cum[-1] + math.hypot(rp[q][0] - rp[q - 1][0], rp[q][1] - rp[q - 1][1]))
            paths.append(PathV(from_id=a, to_id=b, vehicle=v.value, pts=rp, cum=[round(c, 4) for c in cum],
                               len=round(cum[-1], 4)))
            path_idx[k] = len(paths) - 1
        return path_idx[k]

    def get_ent(e) -> _Ent:
        s = ent_state.get(e.entity_id)
        if s is None:
            s = _Ent(len(ents.id))
            ent_state[e.entity_id] = s
            ents.id.append(e.entity_id)
            pid = e.product_id
            if pid not in prod_idx:
                prod_idx[pid] = len(prod_list)
                prod_list.append(ProductV(id=pid, name=pid, color=PRODUCT_PALETTE[len(prod_list) % len(PRODUCT_PALETTE)]))
            ents.product.append(prod_idx[pid])
            ents.batch.append(batch_of.get(pid, 1))
            ents.t_created.append(e.t)
            t_done.append(-1.0)
        return s

    def close(s: _Ent, t: float) -> None:
        if s.open is not None:
            k, t0, ref = s.open
            segs.append((s.idx, k, t0, max(t, t0), ref, -1))
            s.open = None

    def sidx(sid: str | None) -> int:
        if sid not in st_idx:
            raise ValueError(f"Olay logunda yerleşimde olmayan istasyon: {sid!r}.")
        return st_idx[sid]

    def vidx(rid: str | None) -> int:
        if rid not in veh_idx:
            raise ValueError(f"Olay logundaki kaynak {rid!r} simülasyon yapılandırmasındaki araçlarla eşleşmiyor.")
        return veh_idx[rid]

    def emit_transport(s: _Ent, tc: float | None) -> None:
        """TS..TC (veya t_end'de yarım kalan) için kind 4/5/6 + araç görevi."""
        nonlocal scaled_count
        ts, fi, ti, vi = s.pend
        rc = cfg_by_type[s.pend_vtype]
        load, unload = rc.load_time_s, rc.unload_time_s
        nominal = s.pend_dist / rc.speed_mps
        if tc is not None:
            travel = (tc - ts) - load - unload
            if abs(travel - nominal) > 1e-6:
                tot = load + nominal + unload
                f = (tc - ts) / tot if tot > 1e-12 else 0.0
                load, unload, travel = load * f, unload * f, nominal * f
                scaled_count += 1
        else:
            travel = nominal
        a, b = ts + load, ts + load + travel
        c = tc if tc is not None else b + unload
        vparts: list[tuple[int, float, float, int, int]] = []
        for sk, vk, p0, p1, sref in ((SEG_LOAD, VSEG_LOAD, ts, a, fi), (SEG_TRAVEL, VSEG_TRAVEL, a, b, s.pend_route),
                                     (SEG_UNLOAD, VSEG_UNLOAD, b, c, ti)):
            if p0 > t_end:
                continue
            p1 = min(p1, t_end)
            segs.append((s.idx, sk, p0, p1, sref, vi))
            vparts.append((vk, p0, p1, s.pend_route, s.idx))
        tasks.setdefault(vi, []).append(_Task(ts, vparts[-1][2], fi, ti, vparts))
        s.pend = None

    for e in events:
        s = get_ent(e)
        ty = e.type
        if ty == ET.ENTITY_CREATED:
            wip_deltas.append((e.t, 1.0))
            s.open = (SEG_QUEUE, e.t, sidx(e.station_id))   # ilk istasyonda kabul (admission) beklemesi kuyruk sayılır
        elif ty == ET.QUEUE_ENTERED:
            i = sidx(e.station_id)
            if s.open is None or s.open[0] != SEG_QUEUE:
                close(s, e.t)
                s.open = (SEG_QUEUE, e.t, i)
            if e.t >= w0 and e.queue_len is not None and e.queue_len > run_max.get(i, 0):
                run_max[i] = e.queue_len
                max_q.setdefault(i, []).append((e.t, float(e.queue_len)))
        elif ty == ET.PROCESS_STARTED:
            i = sidx(e.station_id)
            close(s, e.t)
            s.open = (SEG_PROCESS, e.t, i)
            if e.resource_id is not None:
                s.work = (e.t, vidx(e.resource_id), i)
        elif ty == ET.PROCESS_FINISHED:
            i = sidx(e.station_id)
            if s.open is not None and s.open[0] == SEG_PROCESS:
                segs.append((s.idx, SEG_PROCESS, s.open[1], e.t, i, s.work[1] if s.work else -1))
                s.open = None
            if s.work is not None:
                wt0, wv, wi = s.work
                tasks.setdefault(wv, []).append(_Task(wt0, e.t, wi, wi, [(VSEG_WORK, wt0, e.t, wi, s.idx)]))
                s.work = None
            s.open = (SEG_BLOCKED, e.t, i)
        elif ty == ET.TRANSPORT_REQUESTED:
            close(s, e.t)
            s.open = (SEG_VEH_WAIT, e.t, sidx(e.from_id))
        elif ty == ET.TRANSPORT_STARTED:
            close(s, e.t)
            fi, ti = sidx(e.from_id), sidx(e.to_id)
            if e.vehicle not in cfg_by_type:
                raise ValueError(f"Taşıma aracı {e.vehicle} simülasyon kaynaklarında tanımlı değil.")
            s.pend = (e.t, fi, ti, vidx(e.resource_id))
            s.pend_route = path_of(e.from_id, e.to_id, e.vehicle)
            s.pend_vtype = e.vehicle
            s.pend_dist = e.distance_m if e.distance_m is not None else routes.get(e.from_id, e.to_id, e.vehicle).distance_m
        elif ty == ET.TRANSPORT_COMPLETED:
            if s.pend is None:
                raise ValueError(f"TRANSPORT_COMPLETED öncesinde STARTED yok: {e.entity_id}")
            emit_transport(s, e.t)
        elif ty == ET.ENTITY_COMPLETED:
            close(s, e.t)
            t_done[s.idx] = e.t
            wip_deltas.append((e.t, -1.0))
            if e.t >= w0:
                comp_deltas.append((e.t, float(ents.batch[s.idx])))
    # ---- yarım kalanlar: t_end'de kapat
    for s in ent_state.values():
        if s.pend is not None:
            emit_transport(s, None)
        if s.work is not None:
            wt0, wv, wi = s.work
            tasks.setdefault(wv, []).append(_Task(wt0, t_end, wi, wi, [(VSEG_WORK, wt0, t_end, wi, s.idx)]))
            segs.append((s.idx, SEG_PROCESS, wt0, t_end, wi, wv))
            s.open = None
            s.work = None
        close(s, t_end)

    segs.sort(key=lambda x: x[2])   # kararlı: t0'a göre
    sg = Segs(ent=[x[0] for x in segs], kind=[x[1] for x in segs], t0=[_r(x[2]) for x in segs],
              t1=[_r(x[3]) for x in segs], ref=[x[4] for x in segs], veh=[x[5] for x in segs])

    # ---- araç segmentleri + ışınlanma (jump)
    vrows: list[tuple[int, int, float, float, int, int, int]] = []   # veh, kind, t0, t1, ref, ent, jump
    teleport = 0
    for vi in range(len(vehicles)):
        tl = sorted(tasks.get(vi, []), key=lambda tk: tk.t0)
        loc, prev_end = -1, 0.0
        for tk in tl:
            if tk.t0 > prev_end:
                vrows.append((vi, VSEG_IDLE, prev_end, tk.t0, loc, -1, 0))
            jump = 1 if (loc != -1 and tk.start_st != loc) else 0
            teleport += jump
            for j, (k, a, b, ref, en) in enumerate(tk.vsegs):
                vrows.append((vi, k, a, b, ref, en, jump if j == 0 else 0))
            loc, prev_end = tk.end_st, max(prev_end, tk.t1)
        if t_end > prev_end or not tl:
            vrows.append((vi, VSEG_IDLE, prev_end, t_end, loc, -1, 0))
    vrows.sort(key=lambda x: x[2])
    vs = VSegs(veh=[x[0] for x in vrows], kind=[x[1] for x in vrows], t0=[_r(x[2]) for x in vrows],
               t1=[_r(x[3]) for x in vrows], ref=[x[4] for x in vrows], ent=[x[5] for x in vrows],
               jump=[x[6] for x in vrows])

    # ---- sayaçlar (series): yuvarlamadan önce ham adım fonksiyonları
    raw: dict[str, _Pts] = {"wip": _from_deltas(wip_deltas), "completed_units": _from_deltas(comp_deltas)}
    qd: dict[int, _Pts] = {}
    bd: dict[int, _Pts] = {}
    for _en, k, a, b, ref, _v in segs:
        if k == SEG_QUEUE:
            qd.setdefault(ref, []).extend([(a, 1.0), (b, -1.0)])
        elif k == SEG_PROCESS:
            bd.setdefault(ref, []).extend([(a, 1.0), (b, -1.0)])
    for i, w in enumerate(layout.workstations):
        raw[f"queue_{w.id}"] = _from_deltas(qd.get(i, []))
        raw[f"busy_{w.id}"] = _from_deltas(bd.get(i, []))
        raw[f"maxq_{w.id}"] = _step_collapse(max_q.get(i, []))
    td: dict[str, _Pts] = {}
    for vi, k, a, b, _ref, _en, _j in vrows:
        if k != VSEG_IDLE:
            td.setdefault(vehicles[vi].type, []).extend([(a, 1.0), (b, -1.0)])
    for rc in cfg.resources:
        raw[f"busy_{rc.vehicle.value}"] = _from_deltas(td.get(rc.vehicle.value, []))

    # ---- tutarlılık (yuvarlanmamış ham veriden)
    cons: dict[str, ConsistencyItem] = {}
    window = dur - w0
    skip_reason = None
    if truncated:
        skip_reason = "kesik log – karşılaştırılmadı"
    elif cropped:
        skip_reason = "pencere kırpıldı – karşılaştırılmadı"
    multi = n_rep > 1
    ref_k = sim.replication_kpis[0] if (multi and sim.replication_kpis) else sim.kpis

    def item(twin: float, simv: float, note: str = "", exact: bool = False) -> ConsistencyItem:
        if skip_reason:
            return ConsistencyItem(twin=twin, sim=simv, ok=None, note=skip_reason)
        ok = twin == simv if exact else math.isclose(twin, simv, rel_tol=1e-9, abs_tol=1e-6)
        return ConsistencyItem(twin=twin, sim=simv, ok=ok, note=note)

    cons["completed_units"] = item(_final(raw["completed_units"]), float(ref_k.completed), "Tamamlanan birim (pencere)", True)
    cons["avg_wip"] = item(_integral(raw["wip"], w0, dur) / window if window > 0 else 0.0, ref_k.avg_wip,
                           "WIP integrali / pencere")
    mq = max((_final(raw[f"maxq_{w.id}"]) for w in layout.workstations), default=0.0)
    cons["max_queue_len"] = item(mq, float(ref_k.max_queue_len), "Pencere maks. queue_len (sim tanımı)", True)
    if not multi:
        stats = {s.station_id: s for s in sim.stations}
        for w in layout.workstations:
            ss = stats.get(w.id)
            if ss is None:
                continue
            u = _integral(raw[f"busy_{w.id}"], w0, dur) / (w.capacity * window) if window > 0 else 0.0
            cons[f"util_{w.id}"] = item(u, ss.utilization, f"{w.id} doluluk")
            cons[f"maxq_{w.id}"] = item(_final(raw[f"maxq_{w.id}"]), float(ss.max_queue_len), f"{w.id} maks. kuyruk", True)
        for rs in sim.resources:
            key = f"busy_{rs.vehicle.value}"
            if key in raw:
                cons[f"busy_s_{rs.vehicle.value}"] = item(_integral(raw[key], w0, dur), rs.busy_s,
                                                           f"{rs.vehicle.value} meşgul süresi (s)")
    else:
        notes.append("Çoklu replikasyon: istasyon/kaynak karşılaştırması yapılmadı (yalnız replikasyon 0 gösterilir).")

    series = {k: _ser(v) for k, v in raw.items()}

    # ---- notlar
    if multi:
        notes.append(f"Replikasyon 0 / {n_rep} gösteriliyor (olay logu yalnız replikasyon 0).")
    if truncated:
        notes.append(f"Olay logu {engine.MAX_EVENTS} kayıtta kesildi; animasyon 0–{t_end:.0f} s ile sınırlı "
                     "(kaydırıcı kilitli, tutarlılık karşılaştırılmadı).")
    if cropped:
        notes.append(f"Pencere kırpıldı: yalnız 0–{t_end:.0f} s gösteriliyor.")
    if teleport:
        notes.append(f"Modellenmeyen boş seyir: {teleport} görev farklı noktadan başladı (ışınlanma).")
    if scaled_count:
        notes.append(f"{scaled_count} taşımada log süresi mesafe/hız ile uyuşmadı; hareket [başlangıç, varış] aralığına ölçeklendi.")
    if routes.mode != "network":
        notes.append(f"Rota modu '{routes.mode}': yaklaşık rota, koridor dışına çıkabilir.")
    notes.extend(n for n in sim.notes if "reddedildi" in n)

    meta = TwinMeta(
        layout_id=layout.id, scenario_label=scenario_label, duration_s=dur, warmup_s=w0, seed=sim.seed,
        replications=n_rep, replication_shown=0, t_end=_r(t_end), truncated=truncated,
        t_valid_end=_r(t_valid_end) if t_valid_end is not None else None,
        bottleneck_station_id=sim.bottleneck_station_id, not_modelled=list(sim.not_modelled), notes=notes,
        teleport_count=teleport, vehicle_colors=vcolors, vehicle_labels=vlabels)
    ents.t_created = [_r(x) for x in ents.t_created]
    ents.t_done = [_r(x) if x >= 0 else -1.0 for x in t_done]
    return TwinData(
        meta=meta,
        scene=Scene(facility=Facility2(w=layout.facility.width, h=layout.facility.height), corridors=corridors_v,
                    obstacles=obstacles_v, stations=stations_v, paths=paths),
        products=prod_list, vehicles=vehicles, entities=ents, segs=sg, vsegs=vs, series=series, consistency=cons)


# --------------------------------------------------------------------------- konum enterpolatörü (JS referansı)
def _along(path: PathV, u: float) -> tuple[float, float]:
    s = min(max(u, 0.0), 1.0) * path.len
    cum = path.cum
    if len(cum) < 2:
        return (path.pts[0][0], path.pts[0][1])
    i = min(max(bisect_right(cum, s) - 1, 0), len(cum) - 2)
    d = cum[i + 1] - cum[i]
    f = 0.0 if d <= 1e-12 else min(max((s - cum[i]) / d, 0.0), 1.0)
    (x0, y0), (x1, y1) = path.pts[i], path.pts[i + 1]
    return (x0 + f * (x1 - x0), y0 + f * (y1 - y0))


def position_at(data: TwinData, t: float) -> dict[str, dict[int, tuple[float, float] | None]]:
    """t anında konumlar: {"entities": {ent_idx: (x,y)} (yalnız taşınanlar),
    "vehicles": {veh_idx: (x,y) | None}} (None = konum modellenmedi / park tepsisi).
    Aralıklar yarı açık [t0, t1); sıfır uzunluklu segmentler atlanır."""
    sc = data.scene
    out_e: dict[int, tuple[float, float]] = {}
    out_v: dict[int, tuple[float, float] | None] = dict.fromkeys(range(len(data.vehicles)))
    sg, vs = data.segs, data.vsegs
    for j in range(len(sg.kind)):
        if sg.kind[j] == SEG_TRAVEL and sg.t0[j] <= t < sg.t1[j]:
            u = (t - sg.t0[j]) / (sg.t1[j] - sg.t0[j])
            out_e[sg.ent[j]] = _along(sc.paths[sg.ref[j]], u)
    for j in range(len(vs.kind)):
        if not (vs.t0[j] <= t < vs.t1[j]):
            continue
        v, k, ref = vs.veh[j], vs.kind[j], vs.ref[j]
        if k == VSEG_TRAVEL:
            out_v[v] = _along(sc.paths[ref], (t - vs.t0[j]) / (vs.t1[j] - vs.t0[j]))
        elif k == VSEG_LOAD:
            p = sc.paths[ref].pts[0]
            out_v[v] = (p[0], p[1])
        elif k == VSEG_UNLOAD:
            p = sc.paths[ref].pts[-1]
            out_v[v] = (p[0], p[1])
        elif ref >= 0:
            p = sc.stations[ref].inp
            out_v[v] = (p[0], p[1])
        else:
            out_v[v] = None
    return {"entities": out_e, "vehicles": out_v}
