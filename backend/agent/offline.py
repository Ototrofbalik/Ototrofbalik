"""[Gemini Integration Engineer] Gemini'siz (offline) mod: deterministik "Bu tesisi analiz et" yolu.

Metindeki TÜM sayılar `FacilityAnalysis` nesnelerinden okunur; hiçbiri burada üretilmez.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from backend.pipeline import FacilityAnalysis, analyze_facility
from backend.schemas import Project

_FACILITY_WORDS = ("tesis", "fabrika", "layout", "yerleşim", "yerlesim", "facility", "factory")


def _norm(text: str) -> str:
    return text.replace("İ", "i").replace("I", "ı").lower()


def is_analyze_request(text: str) -> bool:
    t = _norm(text)
    return ("analiz" in t or "analyze" in t) and any(w in t for w in _FACILITY_WORDS)


def run_offline_analysis(project: Project, out_dir: Path | None = None,
                         analyzer: Callable[..., FacilityAnalysis] = analyze_facility) -> tuple[FacilityAnalysis, list[str]]:
    """Pipeline'ı çalıştırır. Optimizasyon modülü hazır değilse onsuz çalışır ve notu döner."""
    notes: list[str] = []
    try:
        return analyzer(project, optimize=True, out_dir=out_dir), notes
    except NotImplementedError:
        notes.append("Optimizasyon modülü henüz hazır değil; analiz optimizasyon olmadan yapıldı.")
        return analyzer(project, optimize=False, out_dir=out_dir), notes


def summarize_analysis(a: FacilityAnalysis, project: Project, notes: list[str] | None = None) -> str:
    L: list[str] = ["Tesis analizi (Gemini kullanılmadan, yerel hesaplama modülleriyle):"]
    if project.data_label:
        L.append(f"Veri: {project.data_label}")
    v = a.validation
    n_err = sum(1 for i in v.issues if i.severity.value == "error")
    n_warn = len(v.issues) - n_err
    L.append(f"- Layout doğrulama: {'GEÇERLİ' if v.valid else 'GEÇERSİZ'} ({n_err} hata, {n_warn} uyarı).")
    if not v.valid:
        for i in [x for x in v.issues if x.severity.value == "error"][:5]:
            L.append(f"  * {i.code}: {i.message}")
        L.append("Layout geçersiz olduğu için akış, simülasyon ve optimizasyon çalıştırılmadı.")
        return "\n".join(L + _tail(a, notes))
    if a.flow is not None:
        f = a.flow
        L.append(f"- Malzeme akışı: toplam taşıma mesafesi {f.total_distance_m_per_hour:.1f} m/saat "
                 f"(operatör {f.operator_walking_m_per_hour:.1f}, forklift {f.forklift_m_per_hour:.1f}, "
                 f"AGV {f.agv_m_per_hour:.1f} m/saat); ürün başına {f.distance_per_unit_m:.1f} m.")
    if a.baseline_sim is not None:
        k = a.baseline_sim.kpis
        L.append(f"- Simülasyon (seed={a.baseline_sim.seed}, {a.baseline_sim.config.replications} replikasyon): "
                 f"throughput {k.throughput_per_hour:.2f} ürün/saat, ortalama çevrim süresi "
                 f"{k.avg_cycle_time_s:.1f} s, ortalama WIP {k.avg_wip:.2f}.")
        bn = a.bottlenecks.get("bottleneck_station_id") or a.baseline_sim.bottleneck_station_id
        L.append(f"- Darboğaz istasyonu: {bn if bn else 'belirlenemedi'}.")
        if a.baseline_sim.not_modelled:
            L.append("- Modellenmeyenler: " + ", ".join(a.baseline_sim.not_modelled) + ".")
    o = a.optimization
    if o is None:
        L.append("- Optimizasyon: çalıştırılmadı; bu nedenle iyileştirme önerisi/iddiası yoktur.")
    else:
        L.append(f"- Optimizasyon: {len(o.alternatives)} alternatif üretildi"
                 + (f", en iyi: {o.best_alternative_id}" if o.best_alternative_id else ", uygulanabilir en iyi yok") + ".")
        best = next((x for x in o.alternatives if x.id == o.best_alternative_id), None)
        if best is not None:
            L.append(f"  En iyi alternatifte taşıma mesafesi baseline'a göre %{best.distance_reduction_pct:.1f} "
                     f"değişti ({best.flow.total_distance_m_per_hour:.1f} m/saat).")
        if o.target_met is not None:
            L.append(f"  Hedef azaltma {'sağlandı' if o.target_met else 'sağlanamadı'}.")
        L.extend(f"  Not: {n}" for n in o.infeasibility_notes[:3])
    c = a.comparison
    if c is not None:
        for d in c.deltas:
            if d.name in ("throughput_per_hour", "avg_cycle_time_s", "flow.total_distance_m_per_hour"):
                verdict = {True: "iyileşme", False: "kötüleşme", None: "belirsiz"}[d.better]
                pct = f" (%{d.delta_pct:.1f})" if d.delta_pct is not None else ""
                L.append(f"  {d.name}: {d.baseline:.2f} -> {d.scenario:.2f} {d.unit}{pct} — {verdict}")
        if c.savings:
            L.append("  Tasarruf (taşıma maliyeti, saat başına): " + ", ".join(f"{k}={v:.2f}" for k, v in c.savings.items()))
        L.extend(f"  Not: {n}" for n in c.notes[:3])
    return "\n".join(L + _tail(a, notes))


def _tail(a: FacilityAnalysis, notes: list[str] | None) -> list[str]:
    out = [f"Not: {n}" for n in (notes or [])]
    if a.artifacts:
        out.append("Üretilen dosyalar: " + ", ".join(a.artifacts))
    out.append("Adımlar: " + "; ".join(f"{s.step}={'ok' if s.ok else 'hata'}" for s in a.steps))
    return out
