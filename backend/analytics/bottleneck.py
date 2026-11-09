"""[SimPy Engineer] Darboğaz analizi ve senaryo karşılaştırması. Tüm sayılar SimResult'tan hesaplanır."""
from __future__ import annotations

from backend.schemas import (
    FlowMetrics,
    KpiDelta,
    Project,
    ScenarioComparison,
    SimKPIs,
    SimResult,
)

MIN_BOTTLENECK_UTIL = 0.05   # bunun altında hiçbir istasyon darboğaz ilan edilmez
SATURATED_UTIL = 0.85
W_UTIL, W_WAIT, W_QLEN = 0.5, 0.3, 0.2


def analyze_bottlenecks(result: SimResult) -> dict:
    """Utilization + kuyruk bekleme + max kuyruk üzerinden sıralı darboğaz listesi (hesaplanmış sayılarla).

    Skor = 0.5*utilization + 0.3*(avg_queue_wait / max avg_queue_wait) + 0.2*(max_queue_len / max max_queue_len).
    """
    sts = result.stations
    max_wait = max((s.avg_queue_wait_s for s in sts), default=0.0)
    max_q = max((s.max_queue_len for s in sts), default=0)
    ranking = []
    for s in sts:
        score = (
            W_UTIL * s.utilization
            + W_WAIT * (s.avg_queue_wait_s / max_wait if max_wait > 0 else 0.0)
            + W_QLEN * (s.max_queue_len / max_q if max_q > 0 else 0.0)
        )
        ranking.append({
            "station_id": s.station_id, "score": score, "utilization": s.utilization,
            "avg_queue_wait_s": s.avg_queue_wait_s, "max_queue_len": s.max_queue_len,
            "processed": s.processed, "saturated": s.utilization >= SATURATED_UTIL,
        })
    ranking.sort(key=lambda r: (-r["score"], -r["utilization"], r["station_id"]))
    top = ranking[0] if ranking else None
    bid = top["station_id"] if top and top["utilization"] >= MIN_BOTTLENECK_UTIL else None
    for i, r in enumerate(ranking, 1):
        r["rank"] = i
    resource_constraints = [
        {"vehicle": r.vehicle.value, "utilization": r.utilization, "distance_m": r.distance_m,
         "saturated": r.utilization >= SATURATED_UTIL}
        for r in sorted(result.resources, key=lambda r: -r.utilization)
    ]
    k = result.kpis
    breakdown = {
        "processing_s": k.avg_processing_s, "setup_s": k.avg_setup_s, "transport_s": k.avg_transport_s,
        "loadunload_s": k.avg_loadunload_s, "resource_wait_s": k.avg_resource_wait_s, "queue_s": k.avg_queue_s,
    }
    total = sum(breakdown.values())
    shares = {n: (v / total if total > 0 else 0.0) for n, v in breakdown.items()}
    findings: list[str] = []
    if bid:
        t = top
        findings.append(
            f"Darboğaz adayı: {bid} (kullanım %{t['utilization'] * 100:.1f}, ort. kuyruk bekleme "
            f"{t['avg_queue_wait_s']:.0f} s, maks. kuyruk {t['max_queue_len']})."
        )
    for rc in resource_constraints:
        if rc["saturated"]:
            findings.append(f"Kaynak {rc['vehicle']} doygun (kullanım %{rc['utilization'] * 100:.1f}).")
    if total > 0:
        dom = max(shares, key=shares.get)
        findings.append(f"Ürün başına süre dağılımında en büyük pay: {dom} (%{shares[dom] * 100:.1f}).")
    return {
        "bottleneck_station_id": bid,
        "ranking": ranking,
        "resource_constraints": resource_constraints,
        "time_breakdown_s": breakdown,
        "time_breakdown_share": shares,
        "findings": findings,
    }


# ad -> (birim, daha iyi yön: +1 yüksek iyi, -1 düşük iyi, 0 belirsiz)
_KPI_META: dict[str, tuple[str, int]] = {
    "throughput_per_hour": ("adet/saat", +1),
    "completed": ("adet", +1),
    "avg_cycle_time_s": ("s", -1),
    "avg_lead_time_s": ("s", -1),
    "avg_wip": ("adet", -1),
    "avg_queue_wait_s": ("s", -1),
    "max_queue_len": ("adet", -1),
    "total_travel_distance_m": ("m", -1),
    "total_travel_time_s": ("s", -1),
    "avg_processing_s": ("s", 0),
    "avg_setup_s": ("s", 0),
    "avg_transport_s": ("s", -1),
    "avg_loadunload_s": ("s", -1),
    "avg_resource_wait_s": ("s", -1),
    "avg_queue_s": ("s", -1),
}
_FLOW_META = {
    "total_distance_m_per_hour": ("m/saat", -1),
    "distance_per_unit_m": ("m", -1),
    "operator_walking_m_per_hour": ("m/saat", -1),
    "forklift_m_per_hour": ("m/saat", -1),
    "agv_m_per_hour": ("m/saat", -1),
    "transport_time_s_per_hour": ("s/saat", -1),
}


def _delta(name: str, unit: str, direction: int, b: float, s: float) -> KpiDelta:
    d = s - b
    return KpiDelta(
        name=name, unit=unit, baseline=b, scenario=s, delta_abs=d,
        delta_pct=(d / b * 100.0) if b != 0 else None,
        better=None if (direction == 0 or d == 0) else (d > 0) == (direction > 0),
    )


def _travel_cost_per_hour(r: SimResult, project: Project) -> float:
    window_h = (r.config.duration_s - r.config.warmup_s) / 3600.0
    cpm = project.cost.cost_per_meter
    return sum(rs.distance_m * cpm.get(rs.vehicle, 0.0) for rs in r.resources) / window_h


def compare_results(baseline: SimResult, scenario: SimResult, baseline_flow: FlowMetrics | None = None,
                    scenario_flow: FlowMetrics | None = None, project: Project | None = None) -> ScenarioComparison:
    """KPI deltaları; tasarruf yalnız project.cost varsa."""
    deltas = [
        _delta(n, u, d, float(getattr(baseline.kpis, n)), float(getattr(scenario.kpis, n)))
        for n, (u, d) in _KPI_META.items()
        if n in SimKPIs.model_fields
    ]
    notes: list[str] = []
    if baseline_flow is not None and scenario_flow is not None:
        for n, (u, d) in _FLOW_META.items():
            deltas.append(_delta(f"flow.{n}", u, d, float(getattr(baseline_flow, n)), float(getattr(scenario_flow, n))))
    elif baseline_flow is not None or scenario_flow is not None:
        notes.append("Yalnız bir tarafta FlowMetrics var; akış (mesafe) deltaları hesaplanmadı.")
    same_seed = baseline.config.seed == scenario.config.seed
    if not same_seed:
        notes.append("Seed'ler farklı: ortak rastgele sayı (CRN) yok; farklar rastgelelikten de gelebilir.")
    if baseline.config.replications != scenario.config.replications:
        notes.append("Replikasyon sayıları farklı; karşılaştırma dikkatle yorumlanmalı.")
    if (baseline.config.duration_s, baseline.config.warmup_s) != (scenario.config.duration_s, scenario.config.warmup_s):
        notes.append("Süre/ısınma farklı; saatlik normalize edilmiş büyüklükler karşılaştırılmalı.")
    for k in ("avg_cycle_time_s", "throughput_per_hour"):
        bci, sci = baseline.ci95.get(k), scenario.ci95.get(k)
        if bci and sci:
            overlap = bci.lo <= sci.hi and sci.lo <= bci.hi
            notes.append(f"{k}: %95 GA {'örtüşüyor (fark istatistiksel olarak belirsiz)' if overlap else 'örtüşmüyor'}.")
    savings = None
    if project is not None and project.cost is not None:
        b = _travel_cost_per_hour(baseline, project)
        s = _travel_cost_per_hour(scenario, project)
        savings = {"travel_cost_per_hour": b - s, "baseline_travel_cost_per_hour": b, "scenario_travel_cost_per_hour": s}
        notes.append(
            f"Tasarruf yalnız taşıma mesafesi x birim maliyetten ({project.cost.currency}/saat); işgücü maliyeti dahil değil."
            + (" Maliyetler varsayımsaldır." if project.cost.assumed else "")
        )
    return ScenarioComparison(
        baseline_id=baseline.layout_id, scenario_id=scenario.layout_id, deltas=deltas, same_seed=same_seed,
        seed=baseline.config.seed, replications=baseline.config.replications, savings=savings, notes=notes,
    )
