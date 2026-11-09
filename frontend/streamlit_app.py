"""FactoryFlow AI — Streamlit arayüzü. Çalıştırma (repo kökünden): `streamlit run frontend/streamlit_app.py`."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st  # noqa: E402

from backend import services  # noqa: E402
from frontend import ai_panel, state, views  # noqa: E402


def main() -> None:
    st.set_page_config(page_title="FactoryFlow AI", page_icon="🏭", layout="wide")
    ctx = state.load_ctx()

    with st.sidebar:
        st.title("🏭 FactoryFlow AI")
        page = st.radio("Menü", state.PAGES, key="page", label_visibility="collapsed")
        if ctx.project and ctx.project.data_label:
            st.warning(ctx.project.data_label)

    # ---- üst bar
    c = st.columns([2.2, 2.2, 1.3, 1.1, 1.4])
    with c[0]:
        ids = [p["id"] for p in ctx.projects]
        if ids:
            names = {p["id"]: p["name"] for p in ctx.projects}
            st.selectbox("Proje", ids, format_func=names.get, key="project_id")
        else:
            st.caption("Kayıtlı proje yok.")
            if st.button("Demo yükle", key="top_demo"):
                state.action_load_demo()
                st.rerun()
    with c[1]:
        if ctx.project and ctx.layout_versions:
            active = ctx.project.layout.id
            vids = [v["id"] for v in ctx.layout_versions]
            labels = {v["id"]: f"v{v['version']} — {v['name']}" for v in ctx.layout_versions}
            pick = st.selectbox("Aktif senaryo (layout sürümü)", vids, index=vids.index(active),
                                format_func=labels.get, key=f"scenario_{ctx.project.id}_{active}")
            if pick != active:
                ok, _ = state.guarded("Senaryo değiştirme", services.activate_layout_version, ctx.project.id, pick)
                if ok:
                    st.rerun()
    has = ctx.project is not None
    with c[2]:
        st.write("")
        if st.button("Run Simulation", key="top_sim", disabled=not has, width="stretch"):
            if state.action_run_simulation(ctx.project.id):  # type: ignore[union-attr]
                st.rerun()
    with c[3]:
        st.write("")
        if st.button("Optimize", key="top_opt", disabled=not has, width="stretch"):
            if state.action_optimize(ctx.project.id):  # type: ignore[union-attr]
                st.rerun()
    with c[4]:
        st.write("")
        if st.button("Export Report", key="top_export", disabled=not has, width="stretch"):
            if state.generate_report(ctx.project.id, ["HTML", "PDF", "Excel", "PNG"]):  # type: ignore[union-attr]
                st.rerun()
    state.show_notice()

    # ---- orta + sağ panel
    main_col, right_col = st.columns([3.4, 1.4])
    with main_col:
        st.header(page)
        views.PAGE_FUNCS[page](ctx)
        views.bottom_panel(ctx)
    with right_col:
        ai_panel.render_panel(ctx)


main()
