"""Isı haritası hesapları (backend/analytics/heatmaps.py) — demo üzerinde gerçek simülasyonla."""
from __future__ import annotations

import pytest

from backend.analytics.heatmaps import from_to_matrix, station_time_queue, station_time_utilization
from backend.demo.demo_factory import build_demo_project
from backend.schemas import EventType
from backend.services import compute_flow
from backend.simulation import engine
from backend.simulation.engine import run_simulation

ET = EventType


@pytest.fixture(scope="module")
def demo():
    p = build_demo_project()
    routes, metrics = compute_flow(p)
    return p, metrics, run_simulation(p, routes)


def _lens(g):
    return [b - a for a, b in zip(g.bin_edges_s, g.bin_edges_s[1:], strict=False)]


def _weighted(row, g, w0=0.0):
    """Σ değer×uzunluk / (T-w0), yalnız başlangıcı >= w0 olan dilimler (w0 dilim sınırına hizalı)."""
    ls = _lens(g)
    num = sum(v * ln for v, ln, a in zip(row, ls, g.bin_edges_s, strict=False) if a >= w0)
    return num / (g.bin_edges_s[-1] - w0)


def _queue_spans(sim, sid):
    open_, out = {}, []
    for e in sim.events:
        if e.station_id != sid:
            continue
        if e.type == ET.QUEUE_ENTERED:
            open_[e.entity_id] = e.t
        elif e.type == ET.PROCESS_STARTED:
            out.append((open_.pop(e.entity_id), e.t))
    out.extend((t0, sim.config.duration_s) for t0 in open_.values())   # süre sonunda hâlâ bekleyen
    return out


def _indep_avg_queue(sim, sid, w0, w1):
    return sum(max(0.0, min(b, w1) - max(a, w0)) for a, b in _queue_spans(sim, sid)) / (w1 - w0)


def _indep_max_queue(sim, sid, a, b):
    """[a,b) içinde pozitif süre süren en büyük eşzamanlı kuyruk (kaba, bağımsız: aday anlar = aralık uçları)."""
    sp = [(x, y) for x, y in _queue_spans(sim, sid) if y > x]
    cands = {a} | {x for x, _ in sp if a <= x < b} | {y for _, y in sp if a <= y < b}
    best = 0
    for t in cands:
        best = max(best, sum(1 for x, y in sp if x <= t < y))
    return best


@pytest.mark.parametrize("bin_s", [1800.0, 3600.0, 7000.0])
def test_util_matches_engine(demo, bin_s):
    p, _, sim = demo
    g = station_time_utilization(sim, p.layout, bin_s)
    assert g.bin_edges_s[0] == 0 and g.bin_edges_s[-1] == sim.config.duration_s
    assert not g.truncated and g.replication_shown == 0
    st = {s.station_id: s.utilization for s in sim.stations}
    for sid, row in zip(g.station_ids, g.values, strict=True):
        assert all(0.0 <= v <= 1.0 for v in row)
        assert _weighted(row, g) == pytest.approx(st[sid], abs=1e-9), sid


def test_short_last_bin(demo):
    p, _, sim = demo
    g = station_time_utilization(sim, p.layout, 7000.0)
    ls = _lens(g)
    assert len(ls) == 5 and ls[-1] == pytest.approx(28800 - 4 * 7000) and sum(ls) == pytest.approx(28800)


def test_queue_avg_matches_independent(demo):
    p, _, sim = demo
    for bin_s in (1800.0, 7000.0):
        g = station_time_queue(sim, p.layout, bin_s, "avg")
        assert g.metric == "avg_queue" and g.unit == "adet"
        T = sim.config.duration_s
        for sid, row in zip(g.station_ids, g.values, strict=True):
            assert min(row) >= 0
            assert _weighted(row, g) == pytest.approx(_indep_avg_queue(sim, sid, 0.0, T), abs=1e-9)
            for j, v in enumerate(row):
                a, b = g.bin_edges_s[j], g.bin_edges_s[j + 1]
                area = sum(max(0.0, min(y, b) - max(x, a)) for x, y in _queue_spans(sim, sid))
                assert v == pytest.approx(area / (b - a), abs=1e-9)


def test_queue_max_matches_independent(demo):
    p, _, sim = demo
    for bin_s in (1800.0, 7000.0):
        g = station_time_queue(sim, p.layout, bin_s, "max")
        assert g.metric == "max_queue"
        for sid, row in zip(g.station_ids, g.values, strict=True):
            for j, v in enumerate(row):
                assert v >= 0 and v == int(v)
                assert v == _indep_max_queue(sim, sid, g.bin_edges_s[j], g.bin_edges_s[j + 1]), (sid, j)
            assert max(row) == _indep_max_queue(sim, sid, 0.0, sim.config.duration_s)


def test_queue_notes_turkish_definition(demo):
    p, _, sim = demo
    g = station_time_queue(sim, p.layout, 3600.0, "max")
    txt = " ".join(g.notes)
    assert "QUEUE_ENTERED" in txt and "operatör" in txt and "queue_len" in txt
    with pytest.raises(ValueError):
        station_time_queue(sim, p.layout, 3600.0, "median")
    with pytest.raises(ValueError):
        station_time_utilization(sim, p.layout, 0.0)


def test_warmup_run():
    p = build_demo_project()
    p.sim_config.warmup_s = 3600.0
    routes, _ = compute_flow(p)
    sim = run_simulation(p, routes)
    g = station_time_utilization(sim, p.layout, 1800.0)
    assert g.warmup_s == 3600.0
    st = {s.station_id: s.utilization for s in sim.stations}
    for sid, row in zip(g.station_ids, g.values, strict=True):
        assert _weighted(row, g, w0=3600.0) == pytest.approx(st[sid], abs=1e-9), sid
    assert any("Isınma" in n for n in g.notes)


def test_capacity_two_station_normalised():
    p = build_demo_project()
    asm = p.layout.station("asm")
    assert asm.capacity == 2
    routes, _ = compute_flow(p)
    sim = run_simulation(p, routes)
    g = station_time_utilization(sim, p.layout, 3600.0)
    row = g.values[g.station_ids.index("asm")]
    # elle: bağımsız meşguliyet / (2 × dilim)
    open_, busy = {}, [0.0] * len(row)
    for e in sim.events:
        if e.station_id != "asm":
            continue
        if e.type == ET.PROCESS_STARTED:
            open_[e.entity_id] = e.t
        elif e.type == ET.PROCESS_FINISHED:
            a, b = open_.pop(e.entity_id), e.t
            for j in range(len(row)):
                busy[j] += max(0.0, min(b, g.bin_edges_s[j + 1]) - max(a, g.bin_edges_s[j]))
    for a in open_.values():   # yarım kalan işlem süre sonunda kesilir
        for j in range(len(row)):
            busy[j] += max(0.0, g.bin_edges_s[j + 1] - max(a, g.bin_edges_s[j]))
    for j, v in enumerate(row):
        assert v == pytest.approx(busy[j] / (2 * (g.bin_edges_s[j + 1] - g.bin_edges_s[j])), abs=1e-9)
    assert max(row) <= 1.0


def test_no_events_sim(demo):
    p, _, sim = demo
    empty = sim.model_copy(update={"events": []})
    for g in (station_time_utilization(empty, p.layout), station_time_queue(empty, p.layout),
              station_time_queue(empty, p.layout, stat="max")):
        assert len(g.values) == len(p.layout.workstations)
        assert all(v == 0 for row in g.values for v in row)
        assert not g.truncated


def test_truncated_log(monkeypatch):
    monkeypatch.setattr(engine, "MAX_EVENTS", 500)
    p = build_demo_project()
    routes, _ = compute_flow(p)
    sim = run_simulation(p, routes)
    assert len(sim.events) == 500
    for g in (station_time_utilization(sim, p.layout), station_time_queue(sim, p.layout)):
        assert g.truncated and any("kesildi" in n for n in g.notes)
        assert all(0 <= v for row in g.values for v in row)


def test_from_to_demo(demo):
    p, metrics, _ = demo
    m = from_to_matrix(p.layout, metrics)
    assert m.total_metres_per_hour == metrics.total_distance_m_per_hour == pytest.approx(1540.0)
    assert m.mode == metrics.mode == "network"
    assert m.station_ids[:7] == ["raw", "cut", "drill", "asm", "qc", "pack", "ship"]
    ix = {s: i for i, s in enumerate(m.station_ids)}
    assert len(metrics.cells) == 6
    nz = 0
    for c in metrics.cells:
        i, j = ix[c.from_id], ix[c.to_id]
        assert m.moves_per_hour[i][j] == c.moves_per_hour
        assert m.distance_m[i][j] == c.distance_m
        assert m.metres_per_hour[i][j] == c.metres_per_hour
        nz += 1
    assert m.moves_per_hour[ix["raw"]][ix["cut"]] == 11 and m.distance_m[ix["raw"]][ix["cut"]] == 31
    assert sum(map(sum, m.metres_per_hour)) == pytest.approx(1540.0)
    assert sum(1 for r in m.moves_per_hour for v in r if v) == nz == 6
    # boş hücre
    assert m.distance_m[ix["ship"]][ix["raw"]] is None
    assert m.moves_per_hour[ix["ship"]][ix["raw"]] == 0 and m.metres_per_hour[ix["ship"]][ix["raw"]] == 0
    assert len(m.station_names) == len(m.station_ids)


def test_from_to_order_and_mode_note(demo):
    p, metrics, _ = demo
    order = ["ship", "pack"]
    m = from_to_matrix(p.layout, metrics.model_copy(update={"mode": "euclidean"}), order)
    assert m.station_ids[:2] == order and set(m.station_ids) == {w.id for w in p.layout.workstations}
    assert any("yaklaşık" in n for n in m.notes)
    assert not any("yaklaşık" in n for n in from_to_matrix(p.layout, metrics).notes)
