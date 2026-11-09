"""[Frontend] Ortak yardımcılar: servis çağrıları (hata gösterimi), oturum durumu, rapor üretimi.

Hesaplama burada YAPILMAZ; backend.services ve domain modülleri çağrılır.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Repo kökünden `streamlit run frontend/streamlit_app.py` ile import çalışsın.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st  # noqa: E402

from backend import services  # noqa: E402
from backend.routing.engine import compute_flow_metrics, compute_routes, derive_flow_edges  # noqa: E402
from backend.schemas import (  # noqa: E402
    FlowMetrics,
    Layout,
    OptimizationResult,
    Project,
    RouteSet,
    SimResult,
)

REPORT_DIR = _ROOT / "data" / "generated_reports"
PAGES = ["Dashboard", "Layout Designer", "Process Flow", "Spaghetti Analysis", "Simulation", "Optimization",
         "Scenario Comparison", "AI Engineer", "Reports", "Settings"]


@dataclass
class Ctx:
    """Bir script çalışması boyunca paylaşılan, servislerden okunmuş durum."""
    project: Project | None = None
    projects: list[dict[str, Any]] = field(default_factory=list)
    layout_versions: list[dict[str, Any]] = field(default_factory=list)
    sim: SimResult | None = None
    opt: tuple[int, OptimizationResult] | None = None


def load_ctx() -> Ctx:
    ctx = Ctx(projects=services.list_projects())
    ids = [p["id"] for p in ctx.projects]
    pending = st.session_state.pop("_set_project", None)
    if pending in ids:
        st.session_state["project_id"] = pending
    pid = st.session_state.get("project_id")
    if pid not in ids:
        pid = ids[0] if ids else None
        st.session_state["project_id"] = pid
    if pid is None:
        return ctx
    ctx.project = services.get_project(pid)
    ctx.layout_versions = services.list_layout_versions(pid)
    ctx.sim = services.get_latest_sim(pid, ctx.project.layout.id)
    ctx.opt = services.get_latest_optimization(pid)
    return ctx


def explain_error(exc: BaseException) -> str:
    if isinstance(exc, services.ModuleNotReady):
        return f"{exc} (NotImplementedError: modül henüz yazılmadı; sonuç üretilmedi, sahte sonuç gösterilmez.)"
    if isinstance(exc, NotImplementedError):
        return f"Bu işlev henüz uygulanmadı: {exc}"
    return f"{type(exc).__name__}: {exc}"


def guarded(label: str, fn: Any, *args: Any, **kwargs: Any) -> tuple[bool, Any]:
    """Servis çağrısı; hatayı kullanıcıya açık Türkçe mesajla gösterir ve (ok, değer) döner."""
    try:
        return True, fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — UI sınırı: her hata kullanıcıya gösterilir
        st.error(f"{label} başarısız: {explain_error(exc)}")
        return False, None


def flow_bundle(project: Project, layout: Layout | None = None) -> tuple[RouteSet, FlowMetrics]:
    """Rotalar + akış metrikleri (domain fonksiyonları; layout içeriğine göre önbellekli)."""
    lay = layout or project.layout
    key = f"flow:{project.id}:{lay.id}:{hash(lay.model_dump_json())}:{hash(str([e.model_dump() for e in project.flow_edges]))}"
    cache = st.session_state.setdefault("_flow_cache", {})
    if key not in cache:
        edges = project.flow_edges or derive_flow_edges(project)
        routes = compute_routes(lay, edges)
        cache[key] = (routes, compute_flow_metrics(lay, edges, routes, project))
    return cache[key]


def notify(msg: str) -> None:
    st.session_state["_notice"] = msg


def show_notice() -> None:
    msg = st.session_state.pop("_notice", None)
    if msg:
        st.success(msg)


# --------------------------------------------------------------------------- eylemler (üst bar + paneller)
def action_run_simulation(project_id: str, config: Any = None) -> bool:
    with st.spinner("Rotalar hesaplanıyor ve simülasyon çalışıyor..."):
        ok, res = guarded("Simülasyon", services.run_simulation_for, project_id, config)
    if ok:
        run_id, sim = res
        notify(f"Simülasyon tamamlandı (kayıt #{run_id}, seed {sim.seed}): verim {sim.kpis.throughput_per_hour:.2f} adet/saat.")
    return ok


def action_optimize(project_id: str) -> bool:
    cfg = st.session_state.get("opt_config")
    with st.spinner("Optimizasyon çalışıyor..."):
        ok, res = guarded("Optimizasyon", services.run_optimization_for, project_id, cfg)
    if ok:
        opt_id, result = res
        notify(f"Optimizasyon tamamlandı (kayıt #{opt_id}): {len(result.alternatives)} alternatif.")
    return ok


def generate_report(project_id: str, formats: list[str], ai_text: str | None = None) -> bool:
    """Son simülasyon + son optimizasyon ile raporu üretir ve services.save_report ile kaydeder."""
    from backend.reporting import builder

    project = services.get_project(project_id)
    sim = services.get_latest_sim(project_id, project.layout.id)
    if sim is None:
        st.error("Rapor için önce bu layout'ta bir simülasyon çalıştırın (Run Simulation).")
        return False
    latest = services.get_latest_optimization(project_id)
    opt = latest[1] if latest and latest[1].baseline_layout_id == project.layout.id else None
    out = REPORT_DIR / project_id
    try:
        with st.spinner("Rapor hazırlanıyor..."):
            ctx = builder.build_report_context(project, sim, opt, ai_text or None)
            paths: list[Path] = []
            if "HTML" in formats:
                paths.append(builder.export_html(ctx, out))
            if "PDF" in formats:
                paths.append(builder.export_pdf(ctx, out))
            if "Excel" in formats:
                paths.append(builder.export_excel(ctx, out))
            if "PNG" in formats:
                paths.extend(builder.export_png(ctx, out))
    except Exception as exc:  # noqa: BLE001
        st.error(f"Rapor üretimi başarısız: {explain_error(exc)}")
        return False
    for p in paths:
        services.save_report(project_id, p.suffix.lstrip("."), str(p), title=ctx["title"],
                             meta={"seed": sim.seed, "layout_id": project.layout.id,
                                   "with_optimization": opt is not None})
    notify(f"{len(paths)} rapor dosyası üretildi: {out}" + ("" if opt else " (optimizasyon sonucu yok; ilgili bölümler 'çalıştırılmadı' der)"))
    return True


def action_load_demo() -> None:
    ok, p = guarded("Demo yükleme", services.ensure_demo_project)
    if ok:
        st.session_state["_set_project"] = p.id
        notify("Demo proje yüklendi (ÖRNEK / VARSAYIMSAL VERİ).")
