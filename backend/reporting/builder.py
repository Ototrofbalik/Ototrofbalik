"""[Frontend&Reporting Engineer] Rapor üretimi: 14 bölümlük bağlam + HTML/PDF/Excel/PNG çıktıları.

Her sayı kaynak nesnelerden (Project, SimResult, FlowMetrics, OptimizationResult, ScenarioComparison) okunur;
rapor motoru KPI üretmez. Rota/akış/darboğaz/karşılaştırma gerekirse domain fonksiyonları çağrılarak
(compute_routes, compute_flow_metrics, analyze_bottlenecks, compare_results) hesaplanır.
"""
from __future__ import annotations

import importlib.metadata
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import plotly.graph_objects as go

from backend.analytics.bottleneck import analyze_bottlenecks, compare_results
from backend.layout.validation import validate_layout
from backend.routing.engine import compute_flow_metrics, compute_routes, derive_flow_edges
from backend.schemas import (
    FlowMetrics,
    LayoutAlternative,
    OptimizationResult,
    Project,
    RouteSet,
    ScenarioComparison,
    SimResult,
)
from backend.spaghetti.chart import (
    before_after_figure,
    heatmap_figure,
    layout_figure,
    spaghetti_figure,
)

NOT_COMPUTED = "hesaplanamadı"
SECTION_TITLES = [
    "Proje Bilgileri", "Yönetici Özeti", "Mevcut Fabrika Yerleşimi", "Malzeme Akış Analizi", "Spaghetti Chart",
    "Simülasyon Parametreleri", "Simülasyon Sonuçları", "Darboğaz Analizi", "Önerilen Yerleşim",
    "Senaryo Karşılaştırması", "İyileştirme KPI'ları", "AI Mühendislik Önerileri", "Varsayımlar ve Sınırlamalar",
    "Metodoloji ve Tekrarlanabilirlik",
]


def _table(key: str, caption: str, columns: list[str], rows: list[list[Any]]) -> dict:
    return {"key": key, "caption": caption, "columns": columns, "rows": rows}


def _section(no: int, sid: str, *, paragraphs: list[str] | None = None, bullets: list[str] | None = None,
             tables: list[dict] | None = None, figures: list[str] | None = None) -> dict:
    return {"no": no, "id": sid, "title": SECTION_TITLES[no - 1], "paragraphs": paragraphs or [],
            "bullets": bullets or [], "tables": tables or [], "figures": figures or []}


def _fig_utilization(sim: SimResult) -> go.Figure:
    fig = go.Figure(go.Bar(x=[s.station_id for s in sim.stations], y=[s.utilization * 100 for s in sim.stations],
                           marker_color="#2b6cb0"))
    fig.update_layout(title="İstasyon kullanım oranı (%)", yaxis_title="%", plot_bgcolor="white",
                      margin={"l": 50, "r": 20, "t": 50, "b": 40})
    return fig


def _fig_time_breakdown(bn: dict) -> go.Figure:
    tb = bn["time_breakdown_s"]
    fig = go.Figure(go.Bar(x=list(tb.keys()), y=list(tb.values()), marker_color="#dd6b20"))
    fig.update_layout(title="Ürün başına ortalama süre ayrışımı (s)", yaxis_title="s", plot_bgcolor="white",
                      margin={"l": 50, "r": 20, "t": 50, "b": 40})
    return fig


def _fig_deltas(cmp: ScenarioComparison) -> go.Figure:
    ds = [d for d in cmp.deltas if d.delta_pct is not None]
    colors = ["#2f855a" if d.better else ("#c53030" if d.better is False else "#718096") for d in ds]
    fig = go.Figure(go.Bar(y=[d.name for d in ds], x=[d.delta_pct for d in ds], orientation="h", marker_color=colors))
    fig.update_layout(title="KPI değişimi (%): yeşil = iyileşme, kırmızı = kötüleşme, gri = yön belirsiz",
                      xaxis_title="%", plot_bgcolor="white", height=max(320, 26 * len(ds) + 120),
                      margin={"l": 200, "r": 20, "t": 60, "b": 40})
    return fig


def _version(pkg: str) -> str:
    try:
        return importlib.metadata.version(pkg)
    except importlib.metadata.PackageNotFoundError:
        return "bilinmiyor"


def _best(opt: OptimizationResult | None) -> LayoutAlternative | None:
    if opt is None or opt.best_alternative_id is None:
        return None
    return next((a for a in opt.alternatives if a.id == opt.best_alternative_id), None)


def _reduction_pct(base: float, alt: float) -> float | None:
    """(baseline - alternatif) / baseline * 100; pozitif = mesafe azaldı. Kaynak: iki FlowMetrics değeri."""
    return (base - alt) / base * 100.0 if base > 0 else None


def build_report_context(project: Project, baseline_sim: SimResult, opt: OptimizationResult | None,
                         ai_text: str | None = None) -> dict:
    """14 bölümlük bağlam; TÜM sayılar kaynak nesnelerden okunur (hiçbir sayı elle yazılmaz).

    opt=None: optimizasyon çalıştırılmadı (Önerilen Yerleşim / Karşılaştırma bölümleri bunu açıkça söyler).
    """
    layout = project.layout
    figs: dict[str, dict] = {}
    notes_global: list[str] = []
    if baseline_sim.layout_id != layout.id:
        notes_global.append(
            f"UYARI: baseline simülasyonu layout '{baseline_sim.layout_id}' için, proje layout'u '{layout.id}'.")

    validation = validate_layout(layout)
    edges = project.flow_edges or derive_flow_edges(project)
    routes: RouteSet = compute_routes(layout, edges)
    flow: FlowMetrics = compute_flow_metrics(layout, edges, routes, project)
    bn = analyze_bottlenecks(baseline_sim)
    k = baseline_sim.kpis
    best = _best(opt)

    comparison: ScenarioComparison | None = None
    best_routes: RouteSet | None = None
    best_red: float | None = None
    if best is not None:
        best_routes = compute_routes(best.layout, edges)
        best_red = _reduction_pct(flow.total_distance_m_per_hour, best.flow.total_distance_m_per_hour)
        if best.sim is not None:
            comparison = compare_results(baseline_sim, best.sim, flow, best.flow, project)

    numbers: dict[str, Any] = {
        "d_total_m_per_hour": flow.total_distance_m_per_hour,
        "distance_per_unit_m": flow.distance_per_unit_m,
        "throughput_per_hour": k.throughput_per_hour,
        "completed": k.completed,
        "avg_cycle_time_s": k.avg_cycle_time_s,
        "avg_wip": k.avg_wip,
        "bottleneck_station_id": bn["bottleneck_station_id"],
        "seed": baseline_sim.seed,
        "replications": baseline_sim.config.replications,
        "n_events": len(baseline_sim.events),
        "layout_valid": validation.valid,
    }
    if best is not None:
        numbers["best_alternative_id"] = best.id
        numbers["best_d_total_m_per_hour"] = best.flow.total_distance_m_per_hour
        numbers["best_distance_reduction_pct"] = best_red
    if comparison is not None and comparison.savings is not None:
        numbers["travel_cost_saving_per_hour"] = comparison.savings["travel_cost_per_hour"]

    sections: list[dict] = []

    # 1 ---------------------------------------------------------------- Proje bilgileri
    fac = layout.facility
    sections.append(_section(1, "project_info", paragraphs=[
        f"Veri etiketi: {project.data_label}" if project.data_label else "Veri etiketi: (belirtilmemiş)"
    ] + notes_global, tables=[_table("project_info", "Proje", ["Alan", "Değer"], [
        ["Proje kimliği", project.id], ["Proje adı", project.name], ["Açıklama", project.description or "—"],
        ["Demo / örnek veri", project.is_demo], ["Veri etiketi", project.data_label or "—"],
        ["Layout", f"{layout.name} ({layout.id}, v{layout.version})"],
        ["Tesis boyutu (m)", f"{fac.width:g} x {fac.height:g}"], ["İstasyon sayısı", len(layout.workstations)],
        ["Koridor sayısı", len(layout.corridors)], ["Engel sayısı", len(layout.obstacles)],
        ["Ürün sayısı", len(project.products)],
        ["Layout doğrulama", "geçerli" if validation.valid else "GEÇERSİZ"],
        ["Rapor tarihi (UTC)", datetime.now(UTC).strftime("%Y-%m-%d %H:%M")],
    ])]))

    # 2 ---------------------------------------------------------------- Yönetici özeti
    bullets = [
        f"Mevcut durumda toplam taşıma mesafesi {flow.total_distance_m_per_hour:.1f} m/saat "
        f"(ürün başına {flow.distance_per_unit_m:.2f} m; rota modu: {flow.mode}).",
        f"Simülasyon (seed {baseline_sim.seed}, {baseline_sim.config.replications} replikasyon): "
        f"verim {k.throughput_per_hour:.2f} adet/saat, ortalama çevrim süresi {k.avg_cycle_time_s:.1f} s, "
        f"ortalama WIP {k.avg_wip:.2f}.",
        (f"Darboğaz adayı: {bn['bottleneck_station_id']}." if bn["bottleneck_station_id"]
         else "Belirgin bir darboğaz istasyonu tespit edilmedi."),
    ]
    if opt is None:
        bullets.append("Optimizasyon çalıştırılmadı; iyileştirme önerisi yok.")
    elif best is None:
        bullets.append("Optimizasyon çalıştırıldı ancak en iyi alternatif belirlenmedi"
                       + (f" ({'; '.join(opt.infeasibility_notes)})." if opt.infeasibility_notes else "."))
    else:
        red_txt = f"{best_red:.2f}% azalma" if best_red is not None and best_red >= 0 else (
            f"{-best_red:.2f}% ARTIŞ" if best_red is not None else "değişim hesaplanamadı")
        bullets.append(
            f"En iyi alternatif '{best.name}' ({best.id}): taşıma mesafesi "
            f"{flow.total_distance_m_per_hour:.1f} -> {best.flow.total_distance_m_per_hour:.1f} m/saat ({red_txt}).")
        if opt.target_met is not None:
            bullets.append(f"Hedef azaltma {'sağlandı' if opt.target_met else 'SAĞLANAMADI'}.")
    if not validation.valid:
        bullets.append("UYARI: mevcut layout doğrulamadan geçemedi; sonuçlar dikkatle yorumlanmalı.")
    sections.append(_section(2, "executive_summary", bullets=bullets))

    # 3 ---------------------------------------------------------------- Mevcut layout
    figs["layout_current"] = {"caption": "Mevcut yerleşim (çakışmalar kırmızı)",
                              "fig": layout_figure(layout, validation.issues)}
    st_rows = [[w.id, w.name, w.kind.value, w.x, w.y, w.width, w.length, w.rotation.value, w.processing_time_s,
                w.setup_time_s, w.capacity, w.fixed] for w in layout.workstations]
    tables3 = [_table("stations", "İstasyonlar", ["id", "ad", "tür", "x (m)", "y (m)", "genişlik (m)", "uzunluk (m)",
                                                  "dönüş (°)", "işlem (s)", "setup (s)", "kapasite", "sabit"], st_rows)]
    if validation.issues:
        tables3.append(_table("validation", "Doğrulama bulguları", ["kod", "önem", "mesaj", "varlıklar"],
                              [[i.code, i.severity.value, i.message, ", ".join(i.entity_ids)]
                               for i in validation.issues]))
    sections.append(_section(3, "current_layout", paragraphs=[
        f"Doğrulama: {'geçerli' if validation.valid else 'GEÇERSİZ'}, {len(validation.issues)} bulgu."],
        tables=tables3, figures=["layout_current"]))

    # 4 ---------------------------------------------------------------- Malzeme akışı
    ft_rows = [[c.from_id, c.to_id, c.moves_per_hour, c.distance_m, c.metres_per_hour] for c in flow.cells]
    veh_rows = [["Operatör yürüyüşü", flow.operator_walking_m_per_hour], ["Forklift", flow.forklift_m_per_hour],
                ["AGV", flow.agv_m_per_hour], ["Toplam (D_total)", flow.total_distance_m_per_hour]]
    p4 = [f"Taşıma süresi toplamı: {flow.transport_time_s_per_hour:.1f} s/saat. Rota modu: {flow.mode}."]
    if flow.material_handling_cost_per_hour is not None:
        cur = project.cost.currency if project.cost else ""
        p4.append(f"Taşıma maliyeti: {flow.material_handling_cost_per_hour:.2f} {cur}/saat"
                  + (" (varsayımsal birim maliyet)." if project.cost and project.cost.assumed else "."))
    else:
        p4.append(f"Taşıma maliyeti {NOT_COMPUTED} (maliyet verisi yok).")
    if routes.unreachable:
        p4.append("ULAŞILAMAYAN rotalar: " + "; ".join(f"{a}->{b} ({v})" for a, b, v in routes.unreachable))
    sections.append(_section(4, "material_flow", paragraphs=p4, tables=[
        _table("from_to", "From-To tablosu", ["Nereden", "Nereye", "hareket/saat", "mesafe (m)", "m/saat"], ft_rows),
        _table("vehicle_distance", "Araç bazında mesafe (m/saat)", ["Araç", "m/saat"], veh_rows)]))

    # 5 ---------------------------------------------------------------- Spaghetti
    figs["spaghetti_current"] = {"caption": "Spaghetti chart — mevcut durum",
                                 "fig": spaghetti_figure(layout, routes, flow, title="Spaghetti — mevcut durum")}
    figs["heatmap_current"] = {"caption": "Akış yoğunluğu ısı haritası", "fig": heatmap_figure(layout, routes, flow)}
    sections.append(_section(5, "spaghetti", paragraphs=[
        "Çizgi kalınlığı hareket sıklığını, renk aracı gösterir; rotalar koridor ağı üzerindeki gerçek yollardır."
        if routes.mode == "network" else
        f"UYARI: rota modu '{routes.mode}' — çizgiler gerçek koridor yolu DEĞİLDİR."],
        figures=["spaghetti_current", "heatmap_current"]))

    # 6 ---------------------------------------------------------------- Sim parametreleri
    cfg = baseline_sim.config
    sections.append(_section(6, "sim_parameters", tables=[
        _table("sim_config", "Simülasyon konfigürasyonu", ["Parametre", "Değer"], [
            ["Süre (s)", cfg.duration_s], ["Isınma (s)", cfg.warmup_s], ["Seed", cfg.seed],
            ["Replikasyon", cfg.replications], ["Varış süreci", cfg.arrival],
            ["İşlem süresi değişkenliği", cfg.processing_variability]]),
        _table("resources", "Kaynaklar", ["Araç", "adet", "hız (m/s)", "yükleme (s)", "boşaltma (s)"],
               [[r.vehicle.value, r.count, r.speed_mps, r.load_time_s, r.unload_time_s] for r in cfg.resources]),
        _table("products", "Ürünler", ["id", "ad", "varış/saat", "parti", "rota"],
               [[p.id, p.name, p.arrival_rate_per_hour, p.batch_size, " > ".join(s.station_id for s in p.route)]
                for p in project.products])]))

    # 7 ---------------------------------------------------------------- Sim sonuçları
    kpi_rows = [[n, v] for n, v in k.model_dump().items()]
    et = Counter(e.type.value for e in baseline_sim.events)
    t7 = [
        _table("kpis", "KPI'lar (baseline)", ["KPI", "değer"], kpi_rows),
        _table("station_stats", "İstasyon istatistikleri",
               ["istasyon", "kullanım", "ort. kuyruk bekleme (s)", "maks. kuyruk", "işlenen", "ort. işlem (s)",
                "ort. setup (s)"],
               [[s.station_id, s.utilization, s.avg_queue_wait_s, s.max_queue_len, s.processed, s.avg_process_s,
                 s.avg_setup_s] for s in baseline_sim.stations]),
        _table("resource_stats", "Kaynak istatistikleri",
               ["araç", "kullanım", "mesafe (m)", "meşgul (s)", "boş seyir (m)"],
               [[r.vehicle.value, r.utilization, r.distance_m, r.busy_s, r.empty_travel_m]
                for r in baseline_sim.resources]),
        _table("event_summary", "Olay özeti (replikasyon 0)", ["olay tipi", "adet"],
               [[t, n] for t, n in sorted(et.items())]),
    ]
    if baseline_sim.ci95:
        t7.append(_table("ci95", "%95 güven aralıkları", ["KPI", "ortalama", "alt", "üst", "n"],
                         [[n, c.mean, c.lo, c.hi, c.n] for n, c in baseline_sim.ci95.items()]))
    figs["util"] = {"caption": "İstasyon kullanım oranları", "fig": _fig_utilization(baseline_sim)}
    sections.append(_section(7, "sim_results", paragraphs=list(baseline_sim.notes), tables=t7, figures=["util"]))

    # 8 ---------------------------------------------------------------- Darboğaz
    figs["time_breakdown"] = {"caption": "Süre ayrışımı", "fig": _fig_time_breakdown(bn)}
    sections.append(_section(8, "bottleneck", bullets=list(bn["findings"]), tables=[
        _table("bottleneck_ranking", "Darboğaz sıralaması",
               ["sıra", "istasyon", "skor", "kullanım", "ort. kuyruk bekleme (s)", "maks. kuyruk", "doygun"],
               [[r["rank"], r["station_id"], r["score"], r["utilization"], r["avg_queue_wait_s"], r["max_queue_len"],
                 r["saturated"]] for r in bn["ranking"]]),
        _table("resource_constraints", "Kaynak kısıtları", ["araç", "kullanım", "mesafe (m)", "doygun"],
               [[r["vehicle"], r["utilization"], r["distance_m"], r["saturated"]]
                for r in bn["resource_constraints"]])],
        figures=["time_breakdown"]))

    # 9 ---------------------------------------------------------------- Önerilen layout
    if opt is None:
        sections.append(_section(9, "proposed_layout", paragraphs=[
            "Optimizasyon çalıştırılmadı; önerilen yerleşim yok."]))
    else:
        alt_rows = [[a.id, a.name, a.algorithm, a.objective, a.flow.total_distance_m_per_hour,
                     _reduction_pct(flow.total_distance_m_per_hour, a.flow.total_distance_m_per_hour),
                     a.validation.valid, ", ".join(a.moved_station_ids)] for a in opt.alternatives]
        p9 = [opt.rationale] if opt.rationale else []
        p9 += [f"Uygulanabilirlik notu: {n}" for n in opt.infeasibility_notes]
        t9 = [_table("alternatives", "Alternatifler",
                     ["id", "ad", "algoritma", "amaç", "D_total (m/sa)", "mesafe azalması %", "geçerli", "taşınan"],
                     alt_rows)]
        f9: list[str] = []
        if best is None or best_routes is None:
            p9.append("En iyi alternatif belirlenmedi.")
        else:
            moved = []
            for sid in best.moved_station_ids:
                a, b = layout.station(sid), best.layout.station(sid)
                moved.append([sid, a.x, a.y, a.rotation.value, b.x, b.y, b.rotation.value])
            t9.append(_table("moved", "Taşınan istasyonlar", ["istasyon", "eski x", "eski y", "eski dönüş",
                                                              "yeni x", "yeni y", "yeni dönüş"], moved))
            figs["layout_proposed"] = {"caption": f"Önerilen yerleşim — {best.name}",
                                       "fig": layout_figure(best.layout, best.validation.issues)}
            figs["before_after"] = {"caption": "Önce / sonra spaghetti", "fig": before_after_figure(
                (layout, routes, flow), (best.layout, best_routes, best.flow))}
            f9 += ["layout_proposed", "before_after"]
        sections.append(_section(9, "proposed_layout", paragraphs=p9, tables=t9, figures=f9))

    # 10 --------------------------------------------------------------- Senaryo karşılaştırması
    if comparison is None:
        why = "Optimizasyon çalıştırılmadı." if opt is None else (
            "En iyi alternatif yok." if best is None else "En iyi alternatifin simülasyon sonucu yok (sim=None).")
        sections.append(_section(10, "scenario_comparison", paragraphs=[f"Karşılaştırma yapılamadı: {why}"]))
    else:
        figs["kpi_deltas"] = {"caption": "KPI değişimi (%)", "fig": _fig_deltas(comparison)}
        sections.append(_section(10, "scenario_comparison", paragraphs=[
            f"Baseline: {comparison.baseline_id}; senaryo: {comparison.scenario_id}. Aynı seed (CRN): "
            f"{'evet' if comparison.same_seed else 'HAYIR'}; seed {comparison.seed}; "
            f"replikasyon {comparison.replications}."] + list(comparison.notes), tables=[
            _table("comparison", "KPI karşılaştırması",
                   ["KPI", "birim", "baseline", "senaryo", "fark", "fark %", "daha iyi"],
                   [[d.name, d.unit, d.baseline, d.scenario, d.delta_abs, d.delta_pct,
                     "—" if d.better is None else ("evet" if d.better else "hayır")] for d in comparison.deltas])],
            figures=["kpi_deltas"]))

    # 11 --------------------------------------------------------------- İyileştirme KPI'ları
    if comparison is None:
        sections.append(_section(11, "improvement_kpis", paragraphs=[
            "İyileştirme KPI'ları hesaplanamadı: karşılaştırılacak senaryo sonucu yok. "
            f"Tasarruf ve ROI {NOT_COMPUTED}."]))
    else:
        key = {"throughput_per_hour", "avg_cycle_time_s", "avg_wip", "total_travel_distance_m",
               "flow.total_distance_m_per_hour"}
        rows = [[d.name, d.unit, d.baseline, d.scenario, d.delta_pct] for d in comparison.deltas if d.name in key]
        p11: list[str] = []
        t11 = [_table("improvement", "Anahtar KPI'lar", ["KPI", "birim", "baseline", "senaryo", "fark %"], rows)]
        if project.cost is None or comparison.savings is None:
            p11.append(f"Tasarruf ve ROI {NOT_COMPUTED}: proje için maliyet verisi (CostConfig) tanımlı değil.")
        else:
            cur = project.cost.currency
            sv = comparison.savings
            t11.append(_table("savings", f"Taşıma maliyeti tasarrufu ({cur}/saat)", ["kalem", "değer"], [
                ["baseline taşıma maliyeti", sv["baseline_travel_cost_per_hour"]],
                ["senaryo taşıma maliyeti", sv["scenario_travel_cost_per_hour"]],
                ["tasarruf", sv["travel_cost_per_hour"]]]))
            p11.append("Tasarruf yalnız taşıma mesafesi x birim maliyetten hesaplanır."
                       + (" Birim maliyetler VARSAYIMSALDIR." if project.cost.assumed else ""))
            p11.append(f"ROI {NOT_COMPUTED}: yatırım/yeniden yerleşim maliyeti verisi şemada tanımlı değil.")
        sections.append(_section(11, "improvement_kpis", paragraphs=p11, tables=t11, figures=["kpi_deltas"]))

    # 12 --------------------------------------------------------------- AI
    if ai_text:
        from backend.agent.task_logger import mask_text
        ai_text = mask_text(ai_text)
        sections.append(_section(12, "ai_recommendations", paragraphs=[
            ai_text, "Not: AI yorumu yalnız bu raporda hesaplanmış metriklere dayanmalıdır; sayıların kaynağı "
            "raporun tablolarıdır."], bullets=list(bn["findings"])))
    else:
        sections.append(_section(12, "ai_recommendations", paragraphs=[
            "AI önerisi üretilmedi (AI metni sağlanmadı). Aşağıdaki maddeler yalnız hesaplanmış darboğaz "
            "analizinden gelir."], bullets=list(bn["findings"])))

    # 13 --------------------------------------------------------------- Varsayımlar
    ass = [f"Modellenmeyenler: {', '.join(baseline_sim.not_modelled)}."] if baseline_sim.not_modelled else []
    ass += list(baseline_sim.notes)
    if project.data_label:
        ass.append(f"Veri etiketi: {project.data_label}")
    if project.cost is not None and project.cost.assumed:
        ass.append("Maliyet birimleri varsayımsaldır.")
    if project.cost is None:
        ass.append("Maliyet verisi yok: tasarruf/ROI hesaplanmadı.")
    if flow.mode != "network":
        ass.append(f"Akış mesafeleri '{flow.mode}' yaklaşımıyla hesaplandı (gerçek koridor mesafesi değil).")
    if opt is not None:
        ass += [f"Optimizasyon: {n}" for n in opt.infeasibility_notes]
    sections.append(_section(13, "assumptions", bullets=ass))

    # 14 --------------------------------------------------------------- Metodoloji
    meth = [["Baseline seed", baseline_sim.seed], ["Replikasyon", cfg.replications], ["Süre (s)", cfg.duration_s],
            ["Isınma (s)", cfg.warmup_s], ["Rota modu", routes.mode],
            ["Python kütüphaneleri", ", ".join(f"{p} {_version(p)}" for p in
                                               ("simpy", "networkx", "numpy", "plotly", "pydantic"))]]
    if opt is not None:
        oc = opt.config
        meth += [["Optimizasyon algoritması", oc.algorithm], ["Optimizasyon seed", oc.seed],
                 ["İterasyon", oc.iterations], ["Alternatif sayısı", oc.n_alternatives],
                 ["Amaç ağırlıkları", str(oc.weights.model_dump())],
                 ["Alternatifler simüle edildi", oc.simulate_alternatives]]
    if comparison is not None:
        meth += [["Karşılaştırma seed", comparison.seed], ["Ortak rastgele sayı (CRN)", comparison.same_seed]]
    sections.append(_section(14, "methodology", paragraphs=[
        "Aynı proje, aynı seed ve parametrelerle simülasyon ve rotalama yeniden çalıştırıldığında aynı sonuçlar "
        "elde edilir. Hesaplamalar domain modüllerinde yapılır; rapor yalnız bu sonuçları sunar."],
        tables=[_table("methodology", "Parametreler", ["Parametre", "Değer"], meth)]))

    return {
        "title": f"FactoryFlow AI Mühendislik Raporu — {project.name}",
        "project_id": project.id,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "data_label": project.data_label,
        "has_optimization": opt is not None,
        "numbers": numbers,
        "sections": sections,
        "_figures": figs,
        "_png": {},
        "_objects": {"project": project, "baseline_sim": baseline_sim, "opt": opt, "best": best, "flow": flow,
                     "routes": routes, "best_routes": best_routes, "comparison": comparison,
                     "validation": validation},
    }


def export_html(ctx: dict, out_dir: Path) -> Path:
    from backend.reporting.exporters import export_html as _f
    return _f(ctx, out_dir)


def export_pdf(ctx: dict, out_dir: Path) -> Path:
    from backend.reporting.exporters import export_pdf as _f
    return _f(ctx, out_dir)


def export_excel(ctx: dict, out_dir: Path) -> Path:
    from backend.reporting.exporters import export_excel as _f
    return _f(ctx, out_dir)


def export_png(ctx: dict, out_dir: Path) -> list[Path]:
    from backend.reporting.exporters import export_png as _f
    return _f(ctx, out_dir)


def delta_figure(cmp: ScenarioComparison) -> go.Figure:
    """KPI değişim (%) çubuk grafiği (UI ve rapor ortak kullanır)."""
    return _fig_deltas(cmp)
