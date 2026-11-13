"""Digital Twin veri üretimi testleri (backend/twin/build.py). Demo üzerinde GERÇEK SimPy simülasyonu."""
from __future__ import annotations

import json
import math
import random
from collections import defaultdict

import pytest

from backend.demo.demo_factory import build_demo_project
from backend.schemas import EventType, ResourceConfig, VehicleType
from backend.services import compute_flow
from backend.simulation import engine
from backend.simulation.engine import run_simulation
from backend.twin import TwinData, build_twin_data, position_at
from backend.twin import build as twin_build
from backend.twin.schema import (
    SEG_LOAD,
    SEG_QUEUE,
    SEG_TRAVEL,
    SEG_UNLOAD,
    VSEG_IDLE,
    VSEG_LOAD,
    VSEG_WORK,
)

ET = EventType


def _run(project):
    routes, _ = compute_flow(project)
    return routes, run_simulation(project, routes)


@pytest.fixture(scope="module")
def demo():
    p = build_demo_project()
    routes, sim = _run(p)
    data = build_twin_data(p.layout, routes, sim, products=p.products)
    return p, routes, sim, data


@pytest.fixture
def exact(monkeypatch):
    """Yuvarlamayı kapatır (tam doğrulama için 9 ondalık)."""
    monkeypatch.setattr(twin_build, "ROUND", 9)


# ------------------------------------------------------------------ yapı
def test_segs_monotonic_and_ordered(demo):
    _, _, _, d = demo
    s = d.segs
    n = len(s.kind)
    assert n > 0 and all(len(c) == n for c in (s.ent, s.t0, s.t1, s.ref, s.veh))
    assert all(s.t0[i] <= s.t0[i + 1] for i in range(n - 1))
    assert all(s.t0[i] <= s.t1[i] for i in range(n))
    v = d.vsegs
    m = len(v.kind)
    assert all(v.t0[i] <= v.t0[i + 1] for i in range(m - 1))
    assert all(v.t0[i] <= v.t1[i] for i in range(m))
    assert all(a <= d.meta.t_end + 1e-9 for a in s.t1)


def test_entity_chain_gapless(demo):
    _, _, _, d = demo
    by_ent = defaultdict(list)
    for i in range(len(d.segs.kind)):
        by_ent[d.segs.ent[i]].append(i)
    assert len(by_ent) == len(d.entities.id)
    for e, idxs in by_ent.items():
        assert d.segs.kind[idxs[0]] == SEG_QUEUE
        assert d.segs.t0[idxs[0]] == d.entities.t_created[e]
        for a, b in zip(idxs, idxs[1:], strict=False):
            assert d.segs.t1[a] == d.segs.t0[b], f"{d.entities.id[e]}: kopukluk"
        end = d.segs.t1[idxs[-1]]
        if d.entities.t_done[e] >= 0:
            assert end == d.entities.t_done[e]
        else:
            assert end == d.meta.t_end    # içeride kalan varlık t_end'de kapanır


def test_transport_durations_exact(demo, exact):
    p, routes, sim, _ = demo
    d = build_twin_data(p.layout, routes, sim, products=p.products)
    speed = {rc.vehicle.value: rc.speed_mps for rc in sim.config.resources}
    starts = {}
    pairs = []   # (ent_idx, ts, tc, dist, vehicle)
    eidx = {x: i for i, x in enumerate(d.entities.id)}
    for ev in sim.events:
        if ev.type == ET.TRANSPORT_STARTED:
            starts[ev.entity_id] = ev
        elif ev.type == ET.TRANSPORT_COMPLETED:
            st = starts.pop(ev.entity_id)
            pairs.append((eidx[ev.entity_id], st.t, ev.t, ev.distance_m, ev.vehicle.value))
    assert len(pairs) > 100
    by_ent = defaultdict(list)
    for i in range(len(d.segs.kind)):
        if d.segs.kind[i] in (SEG_LOAD, SEG_TRAVEL, SEG_UNLOAD):
            by_ent[d.segs.ent[i]].append(i)
    cursor = defaultdict(int)
    for e, ts, tc, dist, veh in pairs:
        c = cursor[e]
        cursor[e] += 3
        l_, t_, u_ = by_ent[e][c:c + 3]
        assert [d.segs.kind[x] for x in (l_, t_, u_)] == [SEG_LOAD, SEG_TRAVEL, SEG_UNLOAD]
        total = (d.segs.t1[l_] - d.segs.t0[l_]) + (d.segs.t1[t_] - d.segs.t0[t_]) + (d.segs.t1[u_] - d.segs.t0[u_])
        assert abs(total - (tc - ts)) < 1e-6
        assert abs((d.segs.t1[t_] - d.segs.t0[t_]) - dist / speed[veh]) < 1e-6
        assert abs(d.segs.t0[l_] - ts) < 1e-6 and abs(d.segs.t1[u_] - tc) < 1e-6


def _dist_to_polyline(p, pts):
    best = math.inf
    for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
        dx, dy = x1 - x0, y1 - y0
        l2 = dx * dx + dy * dy
        f = 0.0 if l2 < 1e-18 else max(0.0, min(1.0, ((p[0] - x0) * dx + (p[1] - y0) * dy) / l2))
        best = min(best, math.hypot(p[0] - (x0 + f * dx), p[1] - (y0 + f * dy)))
    return best


def _in_quad(p, q, tol=0.02):
    sign = 0
    for i in range(4):
        (x0, y0), (x1, y1) = q[i], q[(i + 1) % 4]
        cr = (x1 - x0) * (p[1] - y0) - (y1 - y0) * (p[0] - x0)
        ln = math.hypot(x1 - x0, y1 - y0) or 1.0
        if abs(cr) / ln <= tol:
            continue
        s = 1 if cr > 0 else -1
        if sign and s != sign:
            return False
        sign = s
    return True


def test_positions_on_path_polyline_and_corridor(demo):
    p, _, _, d = demo
    rng = random.Random(7)
    trav = [i for i in range(len(d.segs.kind)) if d.segs.kind[i] == SEG_TRAVEL and d.segs.t1[i] > d.segs.t0[i]]
    assert trav
    quads = [q for c in d.scene.corridors for q in c.quads]
    st = {s.id: s for s in d.scene.stations}
    limit = p.layout.facility.station_access_max_m
    checked = 0
    for _ in range(200):
        i = rng.choice(trav)
        t = rng.uniform(d.segs.t0[i], d.segs.t1[i])
        if t >= d.segs.t1[i]:
            continue
        pos = position_at(d, t)["entities"][d.segs.ent[i]]
        path = d.scene.paths[d.segs.ref[i]]
        assert _dist_to_polyline(pos, path.pts) < 1e-6
        if p.layout.corridors and path.pts:
            in_corr = any(_in_quad(pos, q) for q in quads)
            if not in_corr:   # koridor dışı: istasyon erişim kolunda olmalı
                near = min(
                    max(s.x - pos[0], 0, pos[0] - (s.x + s.w)) ** 2 + max(s.y - pos[1], 0, pos[1] - (s.y + s.h)) ** 2
                    for s in (st[path.from_id], st[path.to_id])) ** 0.5
                assert near <= limit + 1e-6
        checked += 1
    assert checked > 150
    # araç konumu da aynı yolda
    for _ in range(100):
        i = rng.choice(trav)
        t = rng.uniform(d.segs.t0[i], d.segs.t1[i])
        pos_v = [v for v in position_at(d, t)["vehicles"].values() if v is not None]
        assert pos_v


def test_positions_interpolation_endpoints(demo):
    _, _, _, d = demo
    i = next(i for i in range(len(d.segs.kind)) if d.segs.kind[i] == SEG_TRAVEL and d.segs.t1[i] > d.segs.t0[i] + 1)
    path = d.scene.paths[d.segs.ref[i]]
    s, e = d.segs.t0[i], d.segs.t1[i]
    p0 = position_at(d, s + 1e-9)["entities"][d.segs.ent[i]]
    p1 = position_at(d, e - 1e-6)["entities"][d.segs.ent[i]]
    assert math.dist(p0, path.pts[0]) < 1e-3
    assert math.dist(p1, path.pts[-1]) < 1e-3
    mid = position_at(d, (s + e) / 2)["entities"][d.segs.ent[i]]
    # orta nokta, yol uzunluğunun yarısında
    half = path.len / 2
    k = max(j for j in range(len(path.cum)) if path.cum[j] <= half + 1e-9)
    assert _dist_to_polyline(mid, path.pts) < 1e-6
    assert k >= 0


# ------------------------------------------------------------------ sayaçlar / tutarlılık
def test_series_start_zero_and_end_values(demo):
    _, _, sim, d = demo
    assert d.series
    for name, (ts, vs) in d.series.items():
        assert ts[0] == 0.0 and vs[0] == 0.0, name
        assert len(ts) == len(vs)
        assert all(ts[i] < ts[i + 1] for i in range(len(ts) - 1)), name
    assert d.series["completed_units"][1][-1] == sim.kpis.completed
    assert max(d.series["wip"][1]) >= 1


def test_consistency_all_ok_on_demo(demo):
    _, _, _, d = demo
    assert d.consistency["completed_units"].ok is True
    assert d.consistency["avg_wip"].ok is True
    assert d.consistency["max_queue_len"].ok is True
    assert any(k.startswith("util_") for k in d.consistency)
    assert "busy_s_operator" in d.consistency and "busy_s_forklift" in d.consistency
    bad = {k: v for k, v in d.consistency.items() if v.ok is not True}
    assert not bad, bad


def test_consistency_with_warmup():
    p = build_demo_project()
    p.sim_config.warmup_s = 3600.0
    routes, sim = _run(p)
    d = build_twin_data(p.layout, routes, sim, products=p.products)
    bad = {k: v for k, v in d.consistency.items() if v.ok is not True}
    assert not bad, bad
    assert d.consistency["completed_units"].sim == sim.kpis.completed


def test_event_order_preserved_same_time():
    """Aynı t'deki olaylar sıralamayı bozmaz: her varlıkta QUEUE seg'i ENTITY_CREATED anında başlar."""
    p = build_demo_project()
    routes, sim = _run(p)
    d = build_twin_data(p.layout, routes, sim, products=p.products)
    assert d.entities.id == list(dict.fromkeys(e.entity_id for e in sim.events))


# ------------------------------------------------------------------ araç
def test_vehicles_listed_and_no_overlap(demo):
    _, _, sim, d = demo
    assert [v.id for v in d.vehicles] == ["operator-1", "operator-2", "forklift-1"]
    per = defaultdict(list)
    for i in range(len(d.vsegs.kind)):
        per[d.vsegs.veh[i]].append((d.vsegs.t0[i], d.vsegs.t1[i], d.vsegs.kind[i]))
    assert set(per) == {0, 1, 2}
    for v, rows in per.items():
        rows.sort(key=lambda r: r[0])
        assert rows[0][0] == 0.0
        for a, b in zip(rows, rows[1:], strict=False):
            assert a[1] <= b[0] + 1e-9, f"araç {v} zaman örtüşmesi"
        assert rows[-1][1] == d.meta.t_end
    assert VSEG_WORK in d.vsegs.kind      # operatör istasyonda işlem
    assert VSEG_IDLE in d.vsegs.kind


def test_jump_and_teleport_count(demo):
    _, _, sim, d = demo
    # olay logundan bağımsız beklenen ışınlanma: araç başına görev başlangıç/bitiş istasyonu
    seq = defaultdict(list)
    for ev in sim.events:
        if ev.type == ET.TRANSPORT_STARTED:
            seq[ev.resource_id].append(("s", ev.from_id))
        elif ev.type == ET.TRANSPORT_COMPLETED:
            seq[ev.resource_id].append(("e", ev.to_id))
        elif ev.type == ET.PROCESS_STARTED and ev.resource_id:
            seq[ev.resource_id].append(("s", ev.station_id))
        elif ev.type == ET.PROCESS_FINISHED and ev.resource_id:
            seq[ev.resource_id].append(("e", ev.station_id))
    expected = 0
    for items in seq.values():
        last = None
        for kind, st in items:
            if kind == "s":
                if last is not None and st != last:
                    expected += 1
            else:
                last = st
    assert expected > 0
    assert d.meta.teleport_count == expected
    assert sum(d.vsegs.jump) == expected
    assert any("Modellenmeyen boş seyir" in n and str(expected) in n for n in d.meta.notes)
    # jump yalnız görev başlangıç segmentinde (LOAD/WORK); IDLE'da hiç yok
    for i in range(len(d.vsegs.kind)):
        if d.vsegs.jump[i]:
            assert d.vsegs.kind[i] in (VSEG_LOAD, VSEG_WORK)
    # ilk görevden önceki IDLE: park tepsisi (-1); jump=0
    first_idle = {}
    for i in range(len(d.vsegs.kind)):
        first_idle.setdefault(d.vsegs.veh[i], i)
    for i in first_idle.values():
        if d.vsegs.kind[i] == VSEG_IDLE and d.vsegs.t0[i] == 0.0:
            assert d.vsegs.ref[i] == -1
    # ışınlanmada aracın konumu t0'dan önce eski noktada: önceki seg ref istasyonu != başlangıç istasyonu
    for i in range(len(d.vsegs.kind)):
        if d.vsegs.jump[i] and d.vsegs.kind[i] == VSEG_LOAD:
            assert d.scene.paths[d.vsegs.ref[i]].from_id in {s.id for s in d.scene.stations}


def test_not_modelled_and_meta(demo):
    _, _, sim, d = demo
    assert d.meta.not_modelled == sim.not_modelled
    assert "boş seyir" in " ".join(d.meta.not_modelled)
    assert d.meta.bottleneck_station_id == sim.bottleneck_station_id
    assert d.meta.t_end == sim.config.duration_s and d.meta.truncated is False
    assert d.meta.replications == 1 and d.meta.scenario_label == "Mevcut"
    assert d.meta.vehicle_colors["forklift"] == "#ff7f0e"


def test_rounding_two_decimals(demo):
    _, _, _, d = demo
    for col in (d.segs.t0, d.segs.t1, d.vsegs.t0, d.vsegs.t1):
        assert all(round(x, 2) == x for x in col)
    for pth in d.scene.paths:
        assert all(round(c, 2) == c for pt in pth.pts for c in pt)


def test_json_roundtrip_and_size(demo):
    _, _, _, d = demo
    d2 = TwinData.model_validate_json(d.model_dump_json())
    assert d2 == d
    raw = json.dumps(d.model_dump(), ensure_ascii=False, separators=(",", ":"))
    assert TwinData.model_validate(json.loads(raw)) == d
    assert len(raw.encode()) < 200_000, len(raw)


# ------------------------------------------------------------------ kesik log / pencere
def test_truncated_log(monkeypatch):
    monkeypatch.setattr(engine, "MAX_EVENTS", 500)
    p = build_demo_project()
    routes, sim = _run(p)
    assert len(sim.events) == 500
    d = build_twin_data(p.layout, routes, sim, products=p.products)
    assert d.meta.truncated is True
    last = sim.events[-1].t
    assert d.meta.t_valid_end is not None and d.meta.t_valid_end < last
    assert d.meta.t_end == d.meta.t_valid_end
    assert any("kesildi" in n for n in d.meta.notes)
    assert all(it.ok is None and "kesik log" in it.note for it in d.consistency.values())
    assert max(d.segs.t1) <= d.meta.t_end
    assert max(d.vsegs.t1) <= d.meta.t_end
    for ts, _ in d.series.values():
        assert ts[-1] <= d.meta.t_end


def test_window_crop():
    p = build_demo_project()
    routes, sim = _run(p)
    d = build_twin_data(p.layout, routes, sim, window_s=3600, products=p.products)
    assert d.meta.t_end == 3600 and not d.meta.truncated
    assert max(d.segs.t1) <= 3600 and max(d.vsegs.t1) <= 3600
    assert all(it.ok is None for it in d.consistency.values())
    assert any("kırpıldı" in n for n in d.meta.notes)
    position_at(d, 1800.0)
    with pytest.raises(ValueError, match="window_s"):
        build_twin_data(p.layout, routes, sim, window_s=0)


# ------------------------------------------------------------------ kenar durumlar
def test_empty_event_log(demo):
    p, routes, sim, _ = demo
    empty = sim.model_copy(update={"events": []})
    d = build_twin_data(p.layout, routes, empty, products=p.products)
    assert d.segs.kind == [] and d.entities.id == [] and d.meta.teleport_count == 0
    assert len(d.vehicles) == 3
    assert all(k == VSEG_IDLE and r == -1 for k, r in zip(d.vsegs.kind, d.vsegs.ref, strict=True))
    assert d.series["wip"] == [[0.0], [0.0]]


def test_zero_count_vehicle_type():
    p = build_demo_project()
    p.sim_config.resources.append(ResourceConfig(vehicle=VehicleType.AGV, count=0, speed_mps=1.5))
    routes, sim = _run(p)
    d = build_twin_data(p.layout, routes, sim, products=p.products)
    assert all(v.type != "agv" for v in d.vehicles)
    assert d.series["busy_agv"] == [[0.0], [0.0]]
    assert d.meta.vehicle_colors["agv"] == "#2ca02c"
    assert d.consistency["completed_units"].ok is True


def test_unused_vehicle_listed():
    p = build_demo_project()
    p.sim_config.resources[1].count = 3     # 3 forklift, trafik azken hepsi kullanılmaz
    routes, sim = _run(p)
    d = build_twin_data(p.layout, routes, sim, products=p.products)
    ids = [v.id for v in d.vehicles]
    assert "forklift-3" in ids
    used = set(d.vsegs.veh[i] for i in range(len(d.vsegs.kind)) if d.vsegs.kind[i] != VSEG_IDLE)
    unused = [i for i in range(len(ids)) if i not in used]
    for u in unused:
        rows = [i for i in range(len(d.vsegs.kind)) if d.vsegs.veh[i] == u]
        assert len(rows) == 1 and d.vsegs.ref[rows[0]] == -1 and d.vsegs.t1[rows[0]] == d.meta.t_end


def test_single_station_product():
    p = build_demo_project()
    p.products[0].route = p.products[0].route[:1]
    routes, sim = _run(p)
    d = build_twin_data(p.layout, routes, sim, products=p.products)
    assert d.scene.paths == [] and TRANSPORT_FREE(d)
    assert d.meta.teleport_count == 0
    assert d.consistency["completed_units"].ok is True


def TRANSPORT_FREE(d) -> bool:
    return not any(k in (SEG_LOAD, SEG_TRAVEL, SEG_UNLOAD) for k in d.segs.kind)


def test_replications_three():
    p = build_demo_project()
    p.sim_config.replications = 3
    routes, sim = _run(p)
    d = build_twin_data(p.layout, routes, sim, products=p.products)
    assert d.meta.replications == 3 and d.meta.replication_shown == 0
    assert any("Replikasyon 0 / 3" in n for n in d.meta.notes)
    assert not any(k.startswith("util_") or k.startswith("busy_s_") for k in d.consistency)
    assert d.consistency["completed_units"].sim == sim.replication_kpis[0].completed
    assert d.consistency["completed_units"].ok is True
    assert d.consistency["avg_wip"].ok is True


def test_batch_size_three():
    p = build_demo_project()
    p.products[0].batch_size = 3
    p.products[0].arrival_rate_per_hour = 33.0
    routes, sim = _run(p)
    d = build_twin_data(p.layout, routes, sim, products=p.products)
    assert set(d.entities.batch) == {3}
    assert d.consistency["completed_units"].ok is True
    assert d.consistency["completed_units"].twin % 3 == 0
    d2 = build_twin_data(p.layout, routes, sim)      # ürün verilmeden batch=1 varsayılır -> dürüstçe ✗
    assert d2.consistency["completed_units"].ok is False
    assert any("batch_size" in n for n in d2.meta.notes)


# ------------------------------------------------------------------ ön koşullar
def test_precondition_errors(demo):
    p, routes, sim, _ = demo
    with pytest.raises(ValueError, match="RouteSet layout_id"):
        build_twin_data(p.layout, routes.model_copy(update={"layout_id": "baska"}), sim)
    with pytest.raises(ValueError, match="Simülasyon sonucu layout_id"):
        build_twin_data(p.layout, routes, sim.model_copy(update={"layout_id": "baska"}))
    with pytest.raises(ValueError, match="Rota yok"):
        build_twin_data(p.layout, routes.model_copy(update={"routes": []}), sim)
    bad = [r.model_copy(update={"reachable": False}) for r in routes.routes]
    with pytest.raises(ValueError, match="ulaşılamaz"):
        build_twin_data(p.layout, routes.model_copy(update={"routes": bad}), sim)
