"""[Frontend] 10 sayfa. Her buton gerçek servis/domain fonksiyonuna bağlıdır; sahte sonuç yoktur."""
from __future__ import annotations

import uuid
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

from backend import services
from backend.analytics.bottleneck import analyze_bottlenecks, compare_results
from backend.analytics.heatmap_figures import from_to_figure, time_grid_compare_figure, time_grid_figure
from backend.analytics.heatmaps import from_to_matrix, station_time_queue, station_time_utilization
from backend.config import get_settings
from backend.layout import editing
from backend.layout.validation import validate_layout
from backend.reporting import builder
from backend.routing.engine import compute_routes, derive_flow_edges
from backend.schemas import (
    OptimizationConfig,
    Point,
    Rotation,
    SimConfig,
    StationKind,
    VehicleType,
    Workstation,
)
from backend.spaghetti.chart import before_after_figure, heatmap_figure, layout_figure, spaghetti_figure
from frontend import ai_panel, state
from frontend.state import Ctx

NO_PROJECT = "Aktif proje yok. Üst çubuktan veya Settings sayfasından 'Demo yükle' ile başlayın."


def _need_project(ctx: Ctx) -> bool:
    if ctx.project is None:
        st.warning(NO_PROJECT)
        if st.button("Demo yükle", key=f"demo_{st.session_state.get('page')}"):
            state.action_load_demo()
            st.rerun()
        return False
    return True


def _chart(fig: go.Figure, key: str) -> None:
    st.plotly_chart(fig, key=key, width="stretch")


def route_order(project) -> list[str]:
    """İstasyon sırası: ürün rotalarındaki ilk görülme sırası (From-To ısı haritası için)."""
    order: list[str] = []
    for pr in project.products:
        for step in pr.route:
            if step.station_id not in order:
                order.append(step.station_id)
    return order


def _need_sim(ctx: Ctx) -> bool:
    if ctx.sim is None:
        st.info("Bu layout için kayıtlı simülasyon yok. Üst çubuktan 'Run Simulation' çalıştırın.")
        return False
    return True


# --------------------------------------------------------------------------- Dashboard
def dashboard(ctx: Ctx) -> None:
    if not _need_project(ctx):
        return
    p = ctx.project
    assert p is not None
    if p.data_label:
        st.warning(p.data_label)
    val = validate_layout(p.layout)
    routes, flow = state.flow_bundle(p)
    c = st.columns(4)
    c[0].metric("Layout doğrulama", "Geçerli" if val.valid else "GEÇERSİZ", f"{len(val.issues)} bulgu", delta_color="off")
    c[1].metric("D_total (m/saat)", f"{flow.total_distance_m_per_hour:.1f}")
    if ctx.sim:
        k = ctx.sim.kpis
        c[2].metric("Verim (adet/saat)", f"{k.throughput_per_hour:.2f}")
        c[3].metric("Ort. çevrim süresi (s)", f"{k.avg_cycle_time_s:.0f}")
    else:
        c[2].metric("Verim", "—")
        c[3].metric("Ort. çevrim süresi", "—")
    left, right = st.columns(2)
    with left:
        _chart(layout_figure(p.layout, val.issues), "dash_layout")
    with right:
        _chart(spaghetti_figure(p.layout, routes, flow), "dash_spag")
    if ctx.sim:
        bn = analyze_bottlenecks(ctx.sim)
        st.markdown("**Darboğaz analizi**")
        for f in bn["findings"]:
            st.write(f"- {f}")
        fig = go.Figure(go.Bar(x=[s.station_id for s in ctx.sim.stations],
                               y=[s.utilization * 100 for s in ctx.sim.stations]))
        fig.update_layout(title="İstasyon kullanımı (%)", margin={"t": 40, "b": 30})
        _chart(fig, "dash_util")
    if ctx.opt:
        _, o = ctx.opt
        st.caption(f"Son optimizasyon: {len(o.alternatives)} alternatif, en iyi: {o.best_alternative_id or 'yok'}.")


# --------------------------------------------------------------------------- Layout Designer
def layout_designer(ctx: Ctx) -> None:
    if not _need_project(ctx):
        return
    p = ctx.project
    assert p is not None
    if st.session_state.get("draft_for") != p.layout.id:
        st.session_state["draft_layout"] = p.layout
        st.session_state["draft_for"] = p.layout.id
    draft = st.session_state["draft_layout"]
    dirty = draft != p.layout
    st.caption(f"Düzenlenen layout: {draft.name} (taslak{' — kaydedilmemiş değişiklik var' if dirty else ''}). "
               "Koordinatlar metre; orijin sol-alt; (x, y) istasyonun sol-alt köşesidir.")
    rep = validate_layout(draft)
    left, right = st.columns([3, 2])
    with left:
        _chart(layout_figure(draft, rep.issues), "designer_fig")
    with right:
        (st.success if rep.valid else st.error)(
            f"Doğrulama: {'geçerli' if rep.valid else 'GEÇERSİZ'} — {len(rep.issues)} bulgu")
        for i in rep.issues:
            st.write(f"{'🔴' if i.severity.value == 'error' else '🟡'} **{i.code}** — {i.message}")
    tabs = st.tabs(["İstasyon ekle", "Taşı / döndür", "Sil"])
    ids = [s.id for s in draft.workstations]

    def apply(new_layout, msg: str) -> None:
        st.session_state["draft_layout"] = new_layout
        state.notify(msg)
        st.rerun()

    with tabs[0], st.form("add_ws"):
        c = st.columns(4)
        nid = c[0].text_input("id", key="add_id")
        nname = c[1].text_input("ad", key="add_name")
        kind = c[2].selectbox("tür", [k.value for k in StationKind], index=1, key="add_kind")
        rot = c[3].selectbox("dönüş (°)", [r.value for r in Rotation], key="add_rot")
        c = st.columns(4)
        x = c[0].number_input("x (m)", value=0.0, key="add_x")
        y = c[1].number_input("y (m)", value=0.0, key="add_y")
        w = c[2].number_input("genişlik (m)", min_value=0.1, value=4.0, key="add_w")
        ln = c[3].number_input("uzunluk (m)", min_value=0.1, value=3.0, key="add_l")
        c = st.columns(4)
        proc = c[0].number_input("işlem süresi (s)", min_value=0.0, value=60.0, key="add_proc")
        cap = c[1].number_input("kapasite", min_value=1, value=1, step=1, key="add_cap")
        inx = c[2].number_input("giriş ofset x", value=-1.0, key="add_inx")
        outx = c[3].number_input("çıkış ofset x", value=1.0, key="add_outx")
        if st.form_submit_button("Ekle"):
            try:
                ws = Workstation(id=nid.strip(), name=nname.strip() or nid.strip(), kind=StationKind(kind), x=x, y=y,
                                 width=w, length=ln, rotation=Rotation(rot), processing_time_s=proc, capacity=int(cap),
                                 input_offset=Point(x=inx, y=0.0), output_offset=Point(x=outx, y=0.0))
                if not ws.id:
                    raise ValueError("id boş olamaz.")
                apply(editing.add_workstation(draft, ws), f"'{ws.id}' eklendi.")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Ekleme başarısız: {state.explain_error(exc)}")
    with tabs[1]:
        movable = [s for s in draft.workstations if not s.fixed]
        if not movable:
            st.info("Taşınabilir istasyon yok.")
        else:
            sid = st.selectbox("İstasyon", [s.id for s in movable], key="mv_sid")
            cur = draft.station(sid)
            c = st.columns(3)
            mx = c[0].number_input("x (m)", value=float(cur.x), key=f"mv_x_{sid}")
            my = c[1].number_input("y (m)", value=float(cur.y), key=f"mv_y_{sid}")
            mr = c[2].selectbox("dönüş (°)", [r.value for r in Rotation],
                                index=[r.value for r in Rotation].index(cur.rotation.value), key=f"mv_r_{sid}")
            if st.button("Uygula", key="mv_apply"):
                try:
                    apply(editing.move_workstation(draft, sid, mx, my, mr), f"'{sid}' taşındı/döndürüldü.")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Taşıma başarısız: {state.explain_error(exc)}")
    with tabs[2]:
        if not ids:
            st.info("İstasyon yok.")
        else:
            sid = st.selectbox("Silinecek istasyon", ids, key="del_sid")
            used = [pr.name for pr in p.products if any(s.station_id == sid for s in pr.route)]
            if used:
                st.warning(f"'{sid}' şu ürün rotalarında kullanılıyor: {', '.join(used)}. Silme engellendi.")
            elif st.button("Sil", key="del_apply"):
                try:
                    apply(editing.remove_workstation(draft, sid), f"'{sid}' silindi.")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Silme başarısız: {state.explain_error(exc)}")
    st.divider()
    force = st.checkbox("Geçersiz layout'u yine de kaydet", key="save_invalid", value=False)
    c1, c2 = st.columns(2)
    if c1.button("Taslağı yeni layout sürümü olarak kaydet", key="save_draft", disabled=not dirty):
        if not rep.valid and not force:
            st.error("Layout geçersiz; kaydetmek için düzeltin veya 'yine de kaydet'i işaretleyin.")
        else:
            new = editing.clone_layout(draft, f"{p.layout.id.split('-v')[0]}-v{uuid.uuid4().hex[:6]}",
                                       f"{p.layout.name} (düzenlendi)")
            ok, saved = state.guarded("Layout kaydı", services.save_layout_version, p.id, new)
            if ok:
                state.notify(f"Yeni layout sürümü kaydedildi: v{saved.version} ({saved.id}).")
                st.session_state.pop("draft_for", None)
                st.rerun()
    if c2.button("Taslağı at", key="reset_draft", disabled=not dirty):
        st.session_state["draft_layout"] = p.layout
        st.rerun()


# --------------------------------------------------------------------------- Process Flow
def process_flow(ctx: Ctx) -> None:
    if not _need_project(ctx):
        return
    p = ctx.project
    assert p is not None
    routes, flow = state.flow_bundle(p)
    st.markdown("**Ürün rotaları**")
    st.dataframe([{"ürün": pr.name, "varış/saat": pr.arrival_rate_per_hour, "parti": pr.batch_size,
                   "rota": " → ".join(s.station_id for s in pr.route)} for pr in p.products], hide_index=True)
    c = st.columns(4)
    c[0].metric("D_total (m/saat)", f"{flow.total_distance_m_per_hour:.1f}")
    c[1].metric("Ürün başına (m)", f"{flow.distance_per_unit_m:.2f}")
    c[2].metric("Operatör yürüyüşü (m/sa)", f"{flow.operator_walking_m_per_hour:.1f}")
    c[3].metric("Forklift (m/sa)", f"{flow.forklift_m_per_hour:.1f}")
    st.caption(f"Rota modu: {flow.mode}. Taşıma süresi: {flow.transport_time_s_per_hour:.1f} s/saat. "
               + ("Taşıma maliyeti: "
                  f"{flow.material_handling_cost_per_hour:.2f}/saat." if flow.material_handling_cost_per_hour is not None
                  else "Taşıma maliyeti hesaplanamadı (maliyet verisi yok)."))
    st.markdown("**From-To tablosu**")
    st.dataframe([{"nereden": c.from_id, "nereye": c.to_id, "hareket/sa": c.moves_per_hour, "mesafe (m)": c.distance_m,
                   "m/saat": c.metres_per_hour} for c in flow.cells], hide_index=True)
    st.markdown("**From-To ısı haritası**")
    labels = {"moves": "Hareket sıklığı (hareket/sa)", "metres": "Taşıma yükü (m/sa)", "distance": "Mesafe (m)"}
    value = st.radio("Gösterilen değer", list(labels), format_func=labels.get, horizontal=True, key="pf_value")
    ok, fig = state.guarded("From-To ısı haritası", lambda: from_to_figure(
        from_to_matrix(p.layout, flow, order=route_order(p)), value))
    if ok:
        _chart(fig, "pf_matrix")
    if routes.unreachable:
        st.error("Ulaşılamayan rotalar: " + "; ".join(f"{a}→{b} ({v})" for a, b, v in routes.unreachable))


# --------------------------------------------------------------------------- Spaghetti
def spaghetti(ctx: Ctx) -> None:
    if not _need_project(ctx):
        return
    p = ctx.project
    assert p is not None
    routes, flow = state.flow_bundle(p)
    vehicles = st.multiselect("Araçlar", [v.value for v in VehicleType], default=[v.value for v in VehicleType],
                              key="sp_veh")
    sel = {VehicleType(v) for v in vehicles} or None
    tab1, tab2 = st.tabs(["Spaghetti", "Isı haritası"])
    with tab1:
        _chart(spaghetti_figure(p.layout, routes, flow, vehicles=sel), "sp_fig")
    with tab2:
        _chart(heatmap_figure(p.layout, routes, flow), "sp_heat")
    if ctx.opt and ctx.opt[1].baseline_layout_id == p.layout.id:
        _, o = ctx.opt
        best = next((a for a in o.alternatives if a.id == o.best_alternative_id), None)
        if best:
            st.markdown("**Önce / sonra (en iyi alternatif)**")
            edges = p.flow_edges or derive_flow_edges(p)
            _chart(before_after_figure((p.layout, routes, flow),
                                       (best.layout, compute_routes(best.layout, edges), best.flow)), "sp_ba")


# --------------------------------------------------------------------------- Simulation
def _sim_heatmaps(p, sim) -> None:
    """Saatlik doluluk ve kuyruk ısı haritaları (yalnız kayıtlı simülasyon olayları; replikasyon 0)."""
    st.markdown("**Zaman içinde istasyon ısı haritaları**")
    c = st.columns(2)
    bin_min = c[0].radio("Zaman dilimi", [60, 30], format_func=lambda m: f"{m} dakika", horizontal=True,
                         key="hm_bin")
    stat = c[1].radio("Kuyruk değeri", ["avg", "max"], horizontal=True, key="hm_stat",
                      format_func=lambda x: "Ortalama kuyruk" if x == "avg" else "En büyük kuyruk")
    ok, fig = state.guarded("Doluluk ısı haritası", lambda: time_grid_figure(
        station_time_utilization(sim, p.layout, bin_min * 60.0), "Saatlik doluluk"))
    if ok:
        _chart(fig, "sim_hm_util")
    ok, fig = state.guarded("Kuyruk ısı haritası", lambda: time_grid_figure(
        station_time_queue(sim, p.layout, bin_min * 60.0, stat), "Saatlik kuyruk"))
    if ok:
        _chart(fig, "sim_hm_queue")


def simulation(ctx: Ctx) -> None:
    if not _need_project(ctx):
        return
    p = ctx.project
    assert p is not None
    base = p.sim_config
    with st.form("sim_form"):
        c = st.columns(4)
        dur = c[0].number_input("Süre (saat)", min_value=0.1, value=base.duration_s / 3600, key="sim_dur")
        warm = c[1].number_input("Isınma (saat)", min_value=0.0, value=base.warmup_s / 3600, key="sim_warm")
        seed = c[2].number_input("Seed", value=int(base.seed), step=1, key="sim_seed")
        reps = c[3].number_input("Replikasyon", min_value=1, value=int(base.replications), step=1, key="sim_reps")
        res_inputs = []
        for r in base.resources:
            cc = st.columns(2)
            cnt = cc[0].number_input(f"{r.vehicle.value} adedi", min_value=0, value=int(r.count), step=1,
                                     key=f"sim_cnt_{r.vehicle.value}")
            spd = cc[1].number_input(f"{r.vehicle.value} hızı (m/s)", min_value=0.1, value=float(r.speed_mps),
                                     key=f"sim_spd_{r.vehicle.value}")
            res_inputs.append((r, int(cnt), spd))
        go_ = st.form_submit_button("Simülasyonu çalıştır")
    if go_:
        try:
            cfg: SimConfig = base.model_copy(update={
                "duration_s": dur * 3600, "warmup_s": warm * 3600, "seed": int(seed), "replications": int(reps),
                "resources": [r.model_copy(update={"count": n, "speed_mps": s}) for r, n, s in res_inputs]})
            SimConfig.model_validate(cfg.model_dump())
        except Exception as exc:  # noqa: BLE001
            st.error(f"Geçersiz parametre: {exc}")
        else:
            if state.action_run_simulation(p.id, cfg):
                st.rerun()
    if not _need_sim(ctx):
        return
    sim = ctx.sim
    assert sim is not None
    k = sim.kpis
    c = st.columns(4)
    c[0].metric("Verim (adet/saat)", f"{k.throughput_per_hour:.2f}")
    c[1].metric("Tamamlanan", k.completed)
    c[2].metric("Ort. çevrim (s)", f"{k.avg_cycle_time_s:.0f}")
    c[3].metric("Ort. WIP", f"{k.avg_wip:.2f}")
    st.caption(f"Seed {sim.seed}, replikasyon {sim.config.replications}. Modellenmeyenler: {', '.join(sim.not_modelled)}.")
    for n in sim.notes:
        st.caption(n)
    st.markdown("**İstasyonlar**")
    st.dataframe([s.model_dump() for s in sim.stations], hide_index=True)
    st.markdown("**Kaynaklar**")
    st.dataframe([{**r.model_dump(), "vehicle": r.vehicle.value} for r in sim.resources], hide_index=True)
    if sim.ci95:
        st.markdown("**%95 güven aralıkları**")
        st.dataframe([{"kpi": n, **c_.model_dump()} for n, c_ in sim.ci95.items()], hide_index=True)
    _sim_heatmaps(p, sim)
    runs = services.list_simulation_runs(p.id)
    with st.expander(f"Çalıştırma geçmişi ({len(runs)})"):
        st.dataframe(runs, hide_index=True)


# --------------------------------------------------------------------------- Digital Twin
def _twin_scenarios(ctx: Ctx) -> list[tuple[str, object, object]]:
    """(etiket, layout, sim) listesi: 'Mevcut' + (baseline eşleşiyorsa) simülasyonu dolu optimizasyon alternatifleri."""
    p = ctx.project
    assert p is not None and ctx.sim is not None
    out: list[tuple[str, object, object]] = [("Mevcut", p.layout, ctx.sim)]
    if ctx.opt is not None and ctx.opt[1].baseline_layout_id == p.layout.id:
        for alt in ctx.opt[1].alternatives:
            if alt.sim is not None:
                out.append((f"{alt.name} (mesafe %{alt.distance_reduction_pct:.1f} azalma, aynı seed)", alt.layout, alt.sim))
    return out


def digital_twin(ctx: Ctx) -> None:
    if not _need_project(ctx):
        return
    if not _need_sim(ctx):
        return
    p = ctx.project
    assert p is not None
    try:
        from backend.twin import build_twin_data
        from backend.twin.render import render_twin_html
    except ImportError as exc:
        st.error(f"Digital Twin veri modülü henüz hazır değil ({exc}). Sahte animasyon üretilmez.")
        return
    scenarios = _twin_scenarios(ctx)
    labels = [s[0] for s in scenarios]
    pick = st.selectbox("Senaryo", labels, key="twin_scn") if len(labels) > 1 else labels[0]
    label, layout, sim = scenarios[labels.index(pick)]
    st.caption("Olay logunun yeniden oynatımıdır (yalnız replikasyon 0). Modellenmeyenler oynatıcıda belirtilir.")
    ok, html_doc = state.guarded("Digital Twin", lambda: render_twin_html(
        build_twin_data(layout, state.flow_bundle(p, layout)[0], sim, scenario_label=label.split(" (")[0], products=p.products),
        f"{p.name} - Digital Twin ({label})"))
    if not ok:
        return
    st.components.v1.html(html_doc, height=760, scrolling=False)
    st.download_button("Dijital ikizi HTML olarak indir", data=html_doc.encode("utf-8"),
                       file_name=f"digital_twin_{p.id}.html", mime="text/html", key="twin_dl")
    if st.button("Raporlara kaydet", key="twin_save"):
        out = state.REPORT_DIR / p.id
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"digital_twin_{uuid.uuid4().hex[:8]}.html"
        path.write_text(html_doc, encoding="utf-8")
        ok2, _ = state.guarded("Rapor kaydı", services.save_report, p.id, "twin_html", str(path), f"Digital Twin - {label}")
        if ok2:
            st.success(f"Kaydedildi: {path}")


# --------------------------------------------------------------------------- Optimization
def optimization(ctx: Ctx) -> None:
    if not _need_project(ctx):
        return
    p = ctx.project
    assert p is not None
    algos = ["simulated_annealing", "swap_search", "local_search", "ortools_assignment"]
    with st.form("opt_form"):
        c = st.columns(4)
        algo = c[0].selectbox("Algoritma", algos, key="opt_algo")
        iters = c[1].number_input("İterasyon", min_value=1, value=400, step=50, key="opt_iter")
        seed = c[2].number_input("Seed", value=7, step=1, key="opt_seed")
        nalt = c[3].number_input("Alternatif sayısı", min_value=1, max_value=10, value=3, key="opt_nalt")
        c = st.columns(2)
        target = c[0].number_input("Hedef mesafe azaltma % (0 = hedefsiz)", min_value=0.0, max_value=100.0, value=0.0,
                                   key="opt_target")
        simalt = c[1].checkbox("Alternatifleri simüle et (CRN)", value=True, key="opt_sim")
        submit = st.form_submit_button("Optimizasyonu çalıştır")
    if submit:
        st.session_state["opt_config"] = OptimizationConfig(
            algorithm=algo, iterations=int(iters), seed=int(seed), n_alternatives=int(nalt),
            target_distance_reduction_pct=target or None, simulate_alternatives=simalt)
        if state.action_optimize(p.id):
            st.rerun()
    if ctx.opt is None:
        st.info("Kayıtlı optimizasyon sonucu yok.")
        return
    opt_id, o = ctx.opt
    st.markdown(f"**Son sonuç (kayıt #{opt_id})** — algoritma: {o.config.algorithm}, seed {o.config.seed}")
    if o.rationale:
        st.write(o.rationale)
    for n in o.infeasibility_notes:
        st.warning(n)
    if o.target_met is not None:
        (st.success if o.target_met else st.error)(f"Hedef {'sağlandı' if o.target_met else 'sağlanamadı'}.")
    d0 = o.baseline_flow.total_distance_m_per_hour
    st.dataframe([{"id": a.id, "ad": a.name, "algoritma": a.algorithm, "D_total (m/sa)": a.flow.total_distance_m_per_hour,
                   "baseline D_total": d0, "geçerli": a.validation.valid, "taşınan": ", ".join(a.moved_station_ids),
                   "simüle": a.sim is not None} for a in o.alternatives], hide_index=True)
    if not o.alternatives:
        return
    aid = st.selectbox("Alternatif", [a.id for a in o.alternatives],
                       index=max(0, [a.id for a in o.alternatives].index(o.best_alternative_id))
                       if o.best_alternative_id in [a.id for a in o.alternatives] else 0, key="opt_pick")
    alt = next(a for a in o.alternatives if a.id == aid)
    _chart(layout_figure(alt.layout, alt.validation.issues), "opt_layout")
    if st.button("Bu alternatifi yeni layout sürümü olarak uygula", key="opt_apply", disabled=not alt.validation.valid):
        ok, lay = state.guarded("Alternatif uygulama", services.apply_optimization_alternative, p.id, opt_id, alt.id)
        if ok:
            state.notify(f"Alternatif uygulandı: yeni layout sürümü v{lay.version} ({lay.id}) aktif.")
            st.rerun()


# --------------------------------------------------------------------------- Scenario Comparison
def _occupancy_compare(before_sim, before_layout, after_sim, after_layout, key: str,
                       labels: tuple[str, str] = ("Önce", "Sonra")) -> None:
    ok, fig = state.guarded("Doluluk karşılaştırması", lambda: time_grid_compare_figure(
        station_time_utilization(before_sim, before_layout), station_time_utilization(after_sim, after_layout),
        labels))
    if ok:
        _chart(fig, key)


def scenario_comparison(ctx: Ctx) -> None:
    if not _need_project(ctx):
        return
    p = ctx.project
    assert p is not None
    if ctx.sim is not None and ctx.opt and ctx.opt[1].baseline_layout_id == p.layout.id:
        best = next((a for a in ctx.opt[1].alternatives if a.id == ctx.opt[1].best_alternative_id), None)
        if best is not None and best.sim is not None:
            st.markdown("**Saatlik doluluk: mevcut / en iyi alternatif (ortak renk ölçeği)**")
            _occupancy_compare(ctx.sim, p.layout, best.sim, best.layout, "cmp_hm_best", ("Mevcut", best.name))
    runs = services.list_simulation_runs(p.id)
    if len(runs) < 2:
        st.info("Karşılaştırma için en az iki simülasyon kaydı gerekir (farklı layout sürümleri veya parametrelerle).")
        return
    labels = {r["id"]: f"#{r['id']} — layout {r['layout_id']} — seed {r['seed']} — {r['throughput_per_hour']:.2f}/sa"
              for r in runs}
    c = st.columns(2)
    a = c[0].selectbox("Baseline", list(labels), format_func=labels.get, key="cmp_a")
    b = c[1].selectbox("Senaryo", list(labels), format_func=labels.get, index=len(labels) - 1, key="cmp_b")
    if a == b:
        st.warning("Aynı kayıt seçildi.")
        return
    sa, sb = services.get_simulation(a), services.get_simulation(b)
    pa, pb = services.get_project(p.id, sa.layout_id), services.get_project(p.id, sb.layout_id)
    fa, fb = state.flow_bundle(pa)[1], state.flow_bundle(pb)[1]
    cmp = compare_results(sa, sb, fa, fb, p)
    (st.success if cmp.same_seed else st.warning)(
        f"Ortak rastgele sayı (CRN): {'evet' if cmp.same_seed else 'HAYIR'} — seed {cmp.seed}, replikasyon {cmp.replications}")
    for n in cmp.notes:
        st.caption(n)
    st.dataframe([{"KPI": d.name, "birim": d.unit, "baseline": d.baseline, "senaryo": d.scenario, "fark": d.delta_abs,
                   "fark %": d.delta_pct, "daha iyi": d.better} for d in cmp.deltas], hide_index=True)
    _chart(builder.delta_figure(cmp), "cmp_fig")
    st.markdown("**Saatlik doluluk: baseline / senaryo (ortak renk ölçeği)**")
    _occupancy_compare(sa, pa.layout, sb, pb.layout, "cmp_hm_sel", ("Baseline", "Senaryo"))
    if cmp.savings is None:
        st.info("Tasarruf ve ROI hesaplanamadı: proje maliyet verisi yok.")
    else:
        st.metric("Taşıma maliyeti tasarrufu (/saat)", f"{cmp.savings['travel_cost_per_hour']:.2f}")
        st.caption("ROI hesaplanamadı: yatırım maliyeti verisi tanımlı değil.")


# --------------------------------------------------------------------------- AI Engineer
def ai_engineer(ctx: Ctx) -> None:
    st.markdown("**AI Mühendis**")
    status, detail = ai_panel.gemini_status_text()
    st.write(f"Gemini durumu: **{status}** — {detail}")
    if ai_panel.load_orchestrator() is None:
        st.info("AI modülü henüz hazır değil (backend.agent.orchestrator yok). Aşağıdaki çevrimdışı analiz "
                "deterministiktir ve Gemini gerektirmez.")
    if not _need_project(ctx):
        return
    p = ctx.project
    assert p is not None
    if st.button("Bu tesisi analiz et (çevrimdışı pipeline)", key="ai_page_offline"):
        with st.spinner("Analiz çalışıyor..."):
            ai_panel.offline_analysis(p.id)
        st.rerun()
    for role, text in st.session_state.get("chat", [])[-10:]:
        with st.chat_message(role):
            st.markdown(text)
    st.caption("Sohbet için sağ paneli kullanın.")
    tasks = services.list_agent_tasks(p.id)
    st.markdown(f"**Agent görevleri ({len(tasks)})**")
    st.dataframe(tasks, hide_index=True)


# --------------------------------------------------------------------------- Reports
def reports(ctx: Ctx) -> None:
    if not _need_project(ctx):
        return
    p = ctx.project
    assert p is not None
    fmts = st.multiselect("Biçimler", ["HTML", "PDF", "Excel", "PNG"], default=["HTML", "PDF", "Excel", "PNG"], key="rep_fmt")
    ai_text = st.text_area("AI yorumu (isteğe bağlı; rapora olduğu gibi eklenir)", key="rep_ai", height=80)
    if ctx.sim is None:
        st.info("Rapor için önce bir simülasyon çalıştırın.")
    if ctx.opt is None:
        st.caption("Optimizasyon sonucu yok: ilgili bölümler 'çalıştırılmadı' olarak yazılır.")
    if st.button("Rapor üret", key="rep_go", disabled=not fmts):
        if state.generate_report(p.id, fmts, ai_text):
            st.rerun()
    for r in services.list_reports(p.id)[::-1][:20]:
        path = Path(r["path"])
        if path.exists():
            st.download_button(f"{path.name} ({r['created_at'][:16]})", data=path.read_bytes(), file_name=path.name,
                               key=f"dl_{r['id']}")
        else:
            st.caption(f"{path.name}: dosya bulunamadı")


# --------------------------------------------------------------------------- Settings
def settings_page(ctx: Ctx) -> None:
    s = get_settings()
    status, detail = ai_panel.gemini_status_text()
    st.markdown("**Gemini / AI**")
    st.write(f"Durum: **{status}** — {detail}")
    st.write(f"Model: `{s.gemini_model or '(tanımsız)'}` · istek/görev: {s.gemini_max_requests_per_task} · "
             f"günlük bütçe: {s.gemini_daily_request_budget}")
    st.write(f"Dış veri paylaşım onayı (.env): {'verildi' if s.external_data_consent else 'verilmedi'}")
    st.caption("Ayarlar .env dosyasından okunur; API anahtarı arayüzde gösterilmez veya saklanmaz.")
    st.markdown("**Veritabanı**")
    st.code(s.database_url)
    st.markdown("**Projeler**")
    st.dataframe(ctx.projects, hide_index=True)
    if st.button("Demo yükle", key="settings_demo"):
        state.action_load_demo()
        st.rerun()


PAGE_FUNCS = {
    "Dashboard": dashboard, "Layout Designer": layout_designer, "Process Flow": process_flow,
    "Spaghetti Analysis": spaghetti, "Simulation": simulation, "Digital Twin": digital_twin, "Optimization": optimization,
    "Scenario Comparison": scenario_comparison, "AI Engineer": ai_engineer, "Reports": reports,
    "Settings": settings_page,
}


# --------------------------------------------------------------------------- alt panel
def bottom_panel(ctx: Ctx) -> None:
    st.divider()
    t1, t2, t3 = st.tabs(["KPI tabloları", "Olay logu", "Karşılaştırma sonuçları"])
    with t1:
        if ctx.sim is None:
            st.caption("Simülasyon sonucu yok.")
        else:
            st.dataframe([{"KPI": n, "değer": v} for n, v in ctx.sim.kpis.model_dump().items()], hide_index=True)
    with t2:
        if ctx.sim is None:
            st.caption("Simülasyon sonucu yok.")
        else:
            types = sorted({e.type.value for e in ctx.sim.events})
            pick = st.multiselect("Olay tipi", types, key="ev_types")
            evs = [e for e in ctx.sim.events if not pick or e.type.value in pick][:300]
            st.caption(f"Toplam {len(ctx.sim.events)} olay; ilk {len(evs)} gösteriliyor.")
            st.dataframe([e.model_dump(mode="json") for e in evs], hide_index=True)
    with t3:
        if ctx.opt is None or ctx.project is None:
            st.caption("Optimizasyon sonucu yok.")
        else:
            o = ctx.opt[1]
            best = next((a for a in o.alternatives if a.id == o.best_alternative_id), None)
            if best is None or best.sim is None or ctx.sim is None or o.baseline_layout_id != ctx.project.layout.id:
                st.caption("Karşılaştırılabilir en iyi alternatif/simülasyon yok.")
            else:
                cmp = compare_results(ctx.sim, best.sim, state.flow_bundle(ctx.project)[1], best.flow, ctx.project)
                st.dataframe([{"KPI": d.name, "baseline": d.baseline, "senaryo": d.scenario, "fark %": d.delta_pct}
                              for d in cmp.deltas], hide_index=True)
