"""[Frontend] Sağ panel: Gemini AI asistanı, araç geçmişi, önerilen eylemler, bekleyen onaylar, bağlantı durumu.

AI modülü (backend.agent.orchestrator) yoksa dürüstçe "henüz hazır değil" gösterilir; sahte yanıt üretilmez.
"""
from __future__ import annotations

import importlib
import inspect
from typing import Any

import streamlit as st

from backend import services
from backend.config import get_settings
from frontend import state

_TEXT_PARAMS = ("text", "message", "user_text", "user_message", "prompt", "query")
_METHODS = ("handle_message", "chat", "run_task", "ask", "run", "send_message")


def load_orchestrator() -> Any | None:
    """backend.agent.orchestrator modülünü yükler; yoksa None (ImportError dürüstçe ele alınır)."""
    try:
        return importlib.import_module("backend.agent.orchestrator")
    except ImportError:
        return None


def _find_callable(mod: Any) -> Any | None:
    for cls_name in ("Orchestrator", "AgentOrchestrator"):
        cls = getattr(mod, cls_name, None)
        if cls is not None:
            inst = st.session_state.get("_orch_instance")
            if inst is None:
                inst = cls()
                st.session_state["_orch_instance"] = inst
            for m in _METHODS:
                if callable(getattr(inst, m, None)):
                    return getattr(inst, m)
    for m in _METHODS:
        if callable(getattr(mod, m, None)):
            return getattr(mod, m)
    return None


def ask_agent(text: str, project_id: str | None) -> tuple[Any | None, str | None]:
    """(yanıt, hata). Yanıt AgentReply veya str olabilir; hata Türkçe mesajdır."""
    mod = load_orchestrator()
    if mod is None:
        return None, "AI modülü henüz hazır değil (backend.agent.orchestrator bulunamadı)."
    try:
        fn = _find_callable(mod)
        if fn is None:
            return None, "AI modülü yüklendi ancak sohbet giriş noktası bulunamadı (arayüz uyumsuz)."
        params = inspect.signature(fn).parameters
        kwargs: dict[str, Any] = {}
        text_name = next((p for p in _TEXT_PARAMS if p in params), None)
        if text_name is None:
            return None, "AI giriş noktasının metin parametresi tanınmadı (arayüz uyumsuz)."
        kwargs[text_name] = text
        if "project_id" in params:
            kwargs["project_id"] = project_id
        return fn(**kwargs), None
    except Exception as exc:  # noqa: BLE001
        return None, f"AI isteği başarısız: {state.explain_error(exc)}"


def gemini_status_text() -> tuple[str, str]:
    """(durum, açıklama) — .env ayarlarından; gerçek bağlantı doğrulaması yapılmadıysa söylenir."""
    s = get_settings()
    if not s.gemini_enabled:
        return "Disabled", "Gemini .env ile kapatılmış."
    if not s.gemini_configured:
        return "API Key Missing", "GEMINI_API_KEY tanımlı değil; mühendislik arayüzü ve çevrimdışı analiz çalışır."
    return "Yapılandırıldı", "Anahtar tanımlı; bağlantı bu arayüzde doğrulanmadı."


def _render_reply(reply: Any) -> str:
    text = getattr(reply, "text", None)
    return text if isinstance(text, str) else str(reply)


def offline_analysis(project_id: str) -> None:
    """Deterministik 'Bu tesisi analiz et' (backend.pipeline) — Gemini gerektirmez."""
    from backend.pipeline import analyze_facility

    project = services.get_project(project_id)
    notes: list[str] = []
    try:
        res = analyze_facility(project, optimize=True)
    except NotImplementedError:
        notes.append("Optimizasyon motoru henüz uygulanmadı; analiz optimizasyon olmadan yapıldı.")
        try:
            res = analyze_facility(project, optimize=False)
        except Exception as exc:  # noqa: BLE001
            st.session_state.setdefault("chat", []).append(("assistant", f"Analiz başarısız: {state.explain_error(exc)}"))
            return
    except Exception as exc:  # noqa: BLE001
        st.session_state.setdefault("chat", []).append(("assistant", f"Analiz başarısız: {state.explain_error(exc)}"))
        return
    lines = [f"- {'OK' if s.ok else 'HATA'} {s.step}: {s.detail}" for s in res.steps]
    if res.bottlenecks.get("findings"):
        lines += [f"- {f}" for f in res.bottlenecks["findings"]]
    st.session_state.setdefault("chat", []).append(
        ("assistant", "Çevrimdışı analiz (hesaplanmış sonuçlar):\n" + "\n".join(lines + notes)))


def render_panel(ctx: state.Ctx) -> None:
    pid = ctx.project.id if ctx.project else None
    st.subheader("AI Asistanı")
    status, detail = gemini_status_text()
    mod = load_orchestrator()
    icon = {"Disabled": "⚪", "API Key Missing": "🟠"}.get(status, "🟡")
    st.caption(f"{icon} Gemini: **{status}** — {detail}")
    if mod is None:
        st.info("AI modülü henüz hazır değil. Çevrimdışı analiz kullanılabilir.")

    chat: list[tuple[str, str]] = st.session_state.setdefault("chat", [])
    box = st.container(height=260)
    with box:
        if not chat:
            st.caption("Henüz mesaj yok.")
        for role, text in chat[-20:]:
            with st.chat_message(role):
                st.markdown(text)

    with st.form("ai_form", clear_on_submit=True):
        msg = st.text_area("Mesaj", key="ai_msg", height=70, placeholder="örn. Bu tesisi analiz et")
        sent = st.form_submit_button("Gönder", disabled=(pid is None or mod is None))
    if sent and msg.strip() and pid:
        chat.append(("user", msg.strip()))
        reply, err = ask_agent(msg.strip(), pid)
        if err:
            chat.append(("assistant", f"⚠ {err}"))
        else:
            chat.append(("assistant", _render_reply(reply)))
            pend = getattr(reply, "pending_changes", None)
            if pend:
                st.session_state["pending_changes"] = list(pend)
        st.rerun()

    if pid and st.button("Tesisi analiz et (çevrimdışı)", key="ai_offline"):
        chat.append(("user", "Bu tesisi analiz et"))
        with st.spinner("Analiz çalışıyor..."):
            offline_analysis(pid)
        st.rerun()

    with st.expander("Önerilen eylemler", expanded=True):
        if ctx.project is None:
            st.caption("Önce bir proje yükleyin.")
        else:
            shown = False
            if ctx.sim is None and st.button("Simülasyon çalıştır", key="sugg_sim"):
                state.action_run_simulation(ctx.project.id)
                st.rerun()
            if ctx.sim is None:
                shown = True
            if ctx.opt is None and st.button("Optimize et", key="sugg_opt"):
                state.action_optimize(ctx.project.id)
                st.rerun()
            if ctx.opt is None:
                shown = True
            if not shown:
                st.caption("Bekleyen öneri yok: simülasyon ve optimizasyon sonuçları mevcut.")

    with st.expander("Bekleyen onaylar", expanded=False):
        pend = st.session_state.get("pending_changes", [])
        if not pend:
            st.caption("Onay bekleyen değişiklik yok.")
        for p in pend:
            st.write(f"**{getattr(p, 'tool', '?')}** — {getattr(p, 'description', '')}")
        if pend:
            st.caption("Onay/ret, AI modülünün onay akışı üzerinden yapılır; bu arayüzde onay API'si bağlı değil.")

    with st.expander("Araç çalıştırma geçmişi", expanded=False):
        if pid is None:
            st.caption("Proje yok.")
        else:
            try:
                tasks = services.list_agent_tasks(pid)[-5:]
                rows = []
                for t in tasks:
                    for c in services.get_agent_task(t["id"])["tool_calls"]:
                        rows.append({"görev": t["id"][:8], "araç": c.get("tool"), "durum": c.get("status"),
                                     "süre (ms)": c.get("duration_ms"), "özet": c.get("output_summary", "")})
                if rows:
                    st.dataframe(rows, hide_index=True)
                else:
                    st.caption("Kayıtlı araç çağrısı yok.")
            except Exception as exc:  # noqa: BLE001
                st.caption(f"Geçmiş okunamadı: {state.explain_error(exc)}")
