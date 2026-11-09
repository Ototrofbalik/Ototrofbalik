"""[Optimization Engineer — Opus] Layout optimizasyonu.

Akış:
  1. Baseline: validate_layout + compute_routes(network) + compute_flow_metrics (+ run_simulation).
  2. Aday uzayı: taşınabilir (fixed=False) istasyonlar için ızgara + mevcut slotlar × 4 rotasyon;
     tek-başına uygunluk (sınır/engel/sabit istasyon/güvenlik boşluğu/koridor blokajı/erişim) önbellekli.
  3. Portföy: (algoritma, ağırlık, seed) çalıştırmaları — birincil = config.algorithm + config.weights.
     Değerlendirme: tam (validate+route, önbellekli) ve hızlı vekil (ağ-mesafe APSP önbelleği) — bkz. evaluator.
  4. Her çalıştırmanın en iyi FARKLI durumları toplanır; >= n_alternatives seçilir.
  5. Finalistler editing.move_workstation ile kurulur; tam validate_layout + compute_routes + FlowMetrics;
     simulate_alternatives ise baseline ile AYNI SimConfig/seed (CRN) ile run_simulation.
  6. En iyi alternatif: config ağırlıklı hedefte baseline'dan kesin iyi VE D_total'ı baseline'dan kötü olmayan;
     hedef azaltma verilmişse onu karşılayanlar önceliklidir. Gerekçedeki tüm sayılar nesnelerden okunur.
"""
from __future__ import annotations

from dataclasses import dataclass

from backend.layout.editing import clone_layout, move_workstation
from backend.layout.validation import validate_layout
from backend.optimization.algorithms import ALGORITHMS, RunOutput, Search
from backend.optimization.evaluator import Evaluator, Objective, layout_components
from backend.optimization.placement import Placement, PlacementSpace, r6, state_key
from backend.routing.engine import (
    UnreachableFlowError,
    compute_flow_metrics,
    compute_routes,
    derive_flow_edges,
)
from backend.schemas import (
    LayoutAlternative,
    ObjectiveWeights,
    OptimizationConfig,
    OptimizationResult,
    Project,
    SimResult,
)

EPS = 1e-9
ALGO_LABEL = {
    "swap_search": "Swap araması",
    "local_search": "Yerel arama",
    "simulated_annealing": "Benzetimli tavlama",
    "ortools_assignment": "OR-Tools slot ataması",
}


@dataclass(frozen=True)
class RunSpec:
    algorithm: str
    weights: ObjectiveWeights
    weight_label: str
    seed: int


def _portfolio(cfg: OptimizationConfig) -> list[RunSpec]:
    w = cfg.weights
    dist_only = ObjectiveWeights(distance=1.0, walking=0.0, adjacency=w.adjacency, area=0.0)
    walk = ObjectiveWeights(distance=0.5, walking=1.5, adjacency=w.adjacency, area=0.0)
    compact = ObjectiveWeights(distance=w.distance, walking=w.walking, adjacency=w.adjacency, area=max(w.area, 0.5))
    base = [
        RunSpec(cfg.algorithm, w, "kullanıcı ağırlıkları", cfg.seed),
        RunSpec("ortools_assignment", w, "kullanıcı ağırlıkları", cfg.seed),
        RunSpec("simulated_annealing", walk, "yürüyüş öncelikli", cfg.seed + 1),
        RunSpec("local_search", w, "kullanıcı ağırlıkları", cfg.seed),
        RunSpec("swap_search", w, "kullanıcı ağırlıkları", cfg.seed),
        RunSpec("simulated_annealing", dist_only, "yalnız ağ mesafesi", cfg.seed + 2),
        RunSpec("local_search", compact, "kompakt (alan)", cfg.seed),
    ]
    k = 3
    while len(base) < cfg.n_alternatives + 3:
        base.append(RunSpec("simulated_annealing", w, f"kullanıcı ağırlıkları, seed+{k}", cfg.seed + k))
        k += 1
    out: list[RunSpec] = []
    seen: set = set()
    for r in base:
        sig = (r.algorithm, r.weights.model_dump_json(), r.seed if r.algorithm == "simulated_annealing" else 0)
        if sig not in seen:
            seen.add(sig)
            out.append(r)
    return out


def _wlabel(w: ObjectiveWeights) -> str:
    return f"w(mesafe={w.distance:g}, yürüyüş={w.walking:g}, yakınlık={w.adjacency:g}, alan={w.area:g})"


def _sim(project: Project, routes) -> SimResult:
    from backend.simulation.engine import run_simulation
    return run_simulation(project, routes, project.sim_config)


def optimize_layout(project: Project, config: OptimizationConfig | None = None) -> OptimizationResult:
    """Yalnız validate_layout'tan geçen (error'suz) alternatifler döner. Hedef: ağırlıklı ağ-mesafe.
    En az n_alternatives (kısıtlar izin vermezse infeasibility_notes ile gerekçe). simulate_alternatives=True ise
    her alternatif aynı seed ile (CRN) simülasyondan geçer."""
    cfg = config or OptimizationConfig()
    if cfg.algorithm not in ALGORITHMS:
        raise ValueError(f"Bilinmeyen algoritma '{cfg.algorithm}'; geçerli: {', '.join(ALGORITHMS)}")
    layout = project.layout
    ids = {s.id for s in layout.workstations}
    for a, b in cfg.adjacency_pairs:
        if a not in ids or b not in ids:
            raise ValueError(f"Yakınlık çifti bilinmeyen istasyon içeriyor: ({a}, {b})")
    edges = project.flow_edges or derive_flow_edges(project)
    notes: list[str] = []

    # ---------------------------------------------------------------- baseline
    base_val = validate_layout(layout)
    base_routes = compute_routes(layout, edges)
    try:
        base_flow = compute_flow_metrics(layout, edges, base_routes, project)
    except UnreachableFlowError as e:
        raise ValueError(f"Baseline akış mesafesi hesaplanamıyor (erişilemeyen akış): {e}") from e
    if not base_val.valid:
        notes.append(f"Baseline layout geçersiz ({len(base_val.issues)} hata); karşılaştırma yine de baseline "
                     f"ağ D_total={base_flow.total_distance_m_per_hour:.1f} m/sa'ya göre yapılır.")
    base_sim = _sim(project, base_routes) if cfg.simulate_alternatives else None
    base_comps = layout_components(layout, base_flow, cfg.adjacency_pairs)
    d0 = base_flow.total_distance_m_per_hour
    main_obj = Objective(cfg.weights, base_comps)
    base_main_obj = main_obj(base_comps)

    movable = sorted(s.id for s in layout.workstations if not s.fixed)
    fixed_ids = sorted(s.id for s in layout.workstations if s.fixed)
    result_kwargs = dict(baseline_layout_id=layout.id, baseline_flow=base_flow, baseline_sim=base_sim, config=cfg)
    if not movable:
        notes.append("Taşınabilir (fixed=False) istasyon yok; optimizasyon alanı bulunmuyor.")
        return _finish([], None, notes, cfg, base_flow, base_sim, result_kwargs, base_main_obj, {})

    space = PlacementSpace(layout, movable)
    space.build_candidates()
    ev = Evaluator(project, edges, space, list(cfg.adjacency_pairs))
    base_state: dict[str, Placement] = {s: (space.ws[s].x, space.ws[s].y, space.ws[s].rotation) for s in movable}
    search = Search(ev, base_state, base_comps)

    # ---------------------------------------------------------------- portföy
    runs: list[tuple[RunSpec, RunOutput]] = []
    for spec in _portfolio(cfg):
        obj = Objective(spec.weights, base_comps)
        if spec.algorithm == "swap_search":
            out = search.swap_search(obj, cfg.iterations)
        elif spec.algorithm == "local_search":
            out = search.local_search(obj, cfg.iterations)
        elif spec.algorithm == "simulated_annealing":
            out = search.simulated_annealing(obj, cfg.iterations, spec.seed)
        else:
            out = search.ortools_assignment(obj, spec.seed)
        notes.extend(out.notes)
        runs.append((spec, out))

    # ---------------------------------------------------------------- alternatif seçimi (farklı durumlar)
    base_key = state_key(base_state)
    chosen: list[tuple[RunSpec, dict[str, Placement]]] = []
    used = {base_key}

    def take(depth: int) -> None:
        cands = []
        for order, (spec, out) in enumerate(runs):
            if depth < len(out.results):
                st = out.results[depth][1]
                cands.append((0 if order == 0 else 1, main_obj(ev.exact(st).comps), order, spec, st))
        cands.sort(key=lambda t: t[:3])
        for _, _, _, spec, st in cands:
            if len(chosen) >= cfg.n_alternatives:
                return
            k = state_key(st)
            if k not in used:
                used.add(k)
                chosen.append((spec, st))

    depth = 0
    while len(chosen) < cfg.n_alternatives and any(depth < len(o.results) for _, o in runs):
        take(depth)
        depth += 1

    # ---------------------------------------------------------------- finalistler: tam hesap + CRN simülasyon
    alts: list[LayoutAlternative] = []
    for i, (spec, st) in enumerate(chosen, 1):
        alt_id = f"alt-{i}"
        lay = clone_layout(layout, f"{layout.id}-{alt_id}", f"{layout.name} — {ALGO_LABEL[spec.algorithm]} ({spec.weight_label})")
        moved = []
        for sid in movable:
            ws = layout.station(sid)
            x, y, rot = st[sid]
            if abs(ws.x - x) > 1e-6 or abs(ws.y - y) > 1e-6 or ws.rotation != rot:
                lay = move_workstation(lay, sid, r6(x), r6(y), rot.value)
                moved.append(sid)
        val = validate_layout(lay)
        if not val.valid:   # tam değerlendirme ile aynı fonksiyon; savunma amaçlı
            notes.append(f"{alt_id}: nihai doğrulamada hata ({', '.join(i.code for i in val.issues)}); elendi.")
            continue
        routes = compute_routes(lay, edges)
        try:
            flow = compute_flow_metrics(lay, edges, routes, project)
        except UnreachableFlowError as e:
            notes.append(f"{alt_id}: erişilemeyen akış ({e}); elendi.")
            continue
        sim = _sim(project.model_copy(update={"layout": lay}), routes) if cfg.simulate_alternatives else None
        comps = layout_components(lay, flow, cfg.adjacency_pairs)
        alts.append(LayoutAlternative(
            id=alt_id, name=f"Alternatif {i}: {ALGO_LABEL[spec.algorithm]} — {spec.weight_label} {_wlabel(spec.weights)}",
            algorithm=spec.algorithm, layout=lay, validation=val, objective=main_obj(comps), flow=flow, sim=sim,
            distance_reduction_pct=(d0 - flow.total_distance_m_per_hour) / d0 * 100.0 if d0 > 0 else 0.0,
            moved_station_ids=moved,
        ))

    if len(alts) < cfg.n_alternatives:
        top = ", ".join(f"{c}×{n}" for c, n in ev.reject_reasons.most_common(4)) or "yok"
        notes.append(
            f"İstenen {cfg.n_alternatives} alternatif yerine {len(alts)} farklı geçerli ve (kendi ağırlıklarına göre) "
            f"baseline'dan iyi yerleşim bulundu. {len(runs)} portföy çalıştırması, {ev.n_exact} tam değerlendirme; "
            f"reddedilen adayların en sık nedenleri: {top}. Taşınabilir istasyon: {len(movable)}, "
            f"sabit: {len(fixed_ids)} ({', '.join(fixed_ids) or '-'})."
        )
    stats = {"runs": len(runs), "exact_evals": ev.n_exact}
    return _finish(alts, cfg.target_distance_reduction_pct, notes, cfg, base_flow, base_sim, result_kwargs,
                   base_main_obj, stats)


def _finish(alts, target, notes, cfg, base_flow, base_sim, kw, base_obj, stats) -> OptimizationResult:
    d0 = base_flow.total_distance_m_per_hour
    eligible = [a for a in alts if a.objective < base_obj - EPS and a.flow.total_distance_m_per_hour <= d0 + EPS]
    if target is not None:
        meeting = [a for a in eligible if a.distance_reduction_pct >= target - EPS]
        pool = meeting or eligible
    else:
        pool = eligible
    best = min(pool, key=lambda a: (a.objective, a.flow.total_distance_m_per_hour, a.id)) if pool else None
    target_met = None if target is None else bool(best is not None and best.distance_reduction_pct >= target - EPS)
    rationale = _rationale(alts, best, target, target_met, base_flow, base_sim, base_obj, cfg, stats)
    return OptimizationResult(alternatives=alts, best_alternative_id=best.id if best else None, rationale=rationale,
                              infeasibility_notes=notes, target_met=target_met, **kw)


def _rationale(alts, best, target, target_met, base_flow, base_sim, base_obj, cfg, stats) -> str:
    d0 = base_flow.total_distance_m_per_hour
    parts: list[str] = []
    if stats:
        parts.append(f"{stats['runs']} arama çalıştırması, {stats['exact_evals']} tam (validate+ağ rotası) "
                     "değerlendirme.")
    parts.append(f"Baseline ağ D_total={d0:.1f} m/sa ({base_flow.distance_per_unit_m:.1f} m/ürün), operatör yürüyüşü "
                 f"{base_flow.operator_walking_m_per_hour:.1f} m/sa; ağırlıklı hedef={base_obj:.4f} {_wlabel(cfg.weights)}.")
    if not alts:
        parts.append("Kısıtlar altında baseline'dan iyi, geçerli bir alternatif yerleşim bulunamadı; değişiklik önerilmez.")
    elif best is None:
        bestd = max(alts, key=lambda a: a.distance_reduction_pct)
        parts.append(f"Alternatiflerin hiçbiri hem ağırlıklı hedefte hem ağ D_total'da baseline'dan iyi değil "
                     f"(en yüksek D_total azaltımı {bestd.id}: %{bestd.distance_reduction_pct:.1f}); öneri yapılmaz.")
    else:
        f = best.flow
        parts.append(
            f"Önerilen: {best.id} ({ALGO_LABEL.get(best.algorithm, best.algorithm)}). D_total {d0:.1f} -> "
            f"{f.total_distance_m_per_hour:.1f} m/sa (%{best.distance_reduction_pct:.1f} azalma, "
            f"{f.distance_per_unit_m:.1f} m/ürün); operatör yürüyüşü {base_flow.operator_walking_m_per_hour:.1f} -> "
            f"{f.operator_walking_m_per_hour:.1f} m/sa; forklift {base_flow.forklift_m_per_hour:.1f} -> "
            f"{f.forklift_m_per_hour:.1f} m/sa; ağırlıklı hedef {base_obj:.4f} -> {best.objective:.4f}. "
            f"Taşınan istasyonlar: {', '.join(best.moved_station_ids)}. Doğrulama: {len(best.validation.issues)} hata.")
        if best.sim is not None and base_sim is not None:
            b, s = base_sim.kpis, best.sim.kpis
            parts.append(
                f"Simülasyon (aynı seed={best.sim.seed}, CRN): throughput {b.throughput_per_hour:.2f} -> "
                f"{s.throughput_per_hour:.2f} /sa, ort. lead time {b.avg_lead_time_s:.0f} -> {s.avg_lead_time_s:.0f} s, "
                f"ort. taşıma süresi {b.avg_transport_s:.0f} -> {s.avg_transport_s:.0f} s, ort. kaynak bekleme "
                f"{b.avg_resource_wait_s:.0f} -> {s.avg_resource_wait_s:.0f} s; darboğaz "
                f"{base_sim.bottleneck_station_id} -> {best.sim.bottleneck_station_id}.")
            ru = {r.vehicle.value: r.utilization for r in base_sim.resources}
            rs = {r.vehicle.value: r.utilization for r in best.sim.resources}
            parts.append("Kaynak kullanımı: " + ", ".join(
                f"{v} %{ru[v] * 100:.1f} -> %{rs.get(v, 0.0) * 100:.1f}" for v in sorted(ru)) + ".")
            if s.throughput_per_hour < b.throughput_per_hour - 1e-9:
                parts.append("Uyarı: önerilen yerleşimde simülasyon throughput'u baseline'dan düşük.")
            if best.sim.bottleneck_station_id == base_sim.bottleneck_station_id and base_sim.bottleneck_station_id:
                parts.append(f"Not: darboğaz ({base_sim.bottleneck_station_id}) işlem kapasitesi kaynaklı; "
                             "yerleşim değişikliği tek başına bu kısıtı kaldırmaz.")
    if target is not None:
        best_any = max((a.distance_reduction_pct for a in alts), default=0.0)
        if target_met:
            parts.append(f"Hedef %{target:.1f} D_total azaltımı karşılandı (%{best.distance_reduction_pct:.1f}).")
        else:
            parts.append(
                f"Hedef %{target:.1f} D_total azaltımı KARŞILANAMADI: bulunan geçerli alternatiflerin en yüksek azaltımı "
                f"%{best_any:.1f}. Bu, sezgisel aramanın mevcut kısıtlar (sabit istasyonlar, güvenlik boşluğu, "
                "koridor erişimi, koridor ağı) altında bulduğu en iyi sonuçtur; daha fazlası koridor ağında "
                "değişiklik gerektirebilir.")
    return " ".join(parts)
