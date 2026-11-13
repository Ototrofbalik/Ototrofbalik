"""[QA] Kontrol 6 — ölü buton avı. (a) statik: her st.button/form_submit_button işleyicisi servis/domain çağrısı içerir;
(b) dinamik: Streamlit AppTest ile düğmelere basılır ve DB/dosya yan etkileri doğrulanır; 10 sayfa gezilir."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from backend import services
from backend.database.session import configure_database
from frontend import state as fstate
from frontend.state import PAGES
from tests.qa.conftest import REPO

APP = str(REPO / "frontend" / "streamlit_app.py")
REAL_CALL_ROOTS = {"state", "services", "editing", "ai_panel", "agent", "builder", "orchestrator"}
UI_ONLY_BUTTONS = {"reset_draft"}   # bilinçli: oturumdaki layout taslağını kayıtlı layout'a sıfırlar
REAL_CALL_NAMES = {"apply", "ask_agent", "offline_analysis", "action_run_simulation", "action_optimize",
                   "generate_report", "action_load_demo", "guarded"}


# ----------------------------------------------------------------------------------------- (a) statik
def _button_calls(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in (
                "button", "form_submit_button", "download_button"):
            yield n


def _calls_in(nodes) -> list[str]:
    out = []
    for b in nodes:
        for n in ast.walk(b):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Attribute):
                    root = f
                    while isinstance(root, ast.Attribute):
                        root = root.value
                    out.append(f"{getattr(root, 'id', '?')}.{f.attr}")
                elif isinstance(f, ast.Name):
                    out.append(f.id)
    return out


def _is_real(calls: list[str]) -> bool:
    return any(c.split(".")[0] in REAL_CALL_ROOTS or c in REAL_CALL_NAMES for c in calls)


def test_every_button_handler_calls_a_real_service():
    unbound, dead = [], []
    total = 0
    for path in sorted((REPO / "frontend").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        ifs = [n for n in ast.walk(tree) if isinstance(n, ast.If)]
        for call in _button_calls(tree):
            total += 1
            where = f"{path.name}:{call.lineno}"
            if call.func.attr == "download_button":
                has_data = any(k.arg == "data" for k in call.keywords)
                if not has_data:
                    dead.append(f"{where} download_button veri yok")
                continue
            # düğme doğrudan bir If testinde mi, yoksa bir değişkene mi atanıyor?
            handler = None
            for i in ifs:
                if any(c is call for c in ast.walk(i.test)):
                    handler = i
                    break
            if handler is None:
                p = parents.get(call)
                names = []
                if isinstance(p, ast.Assign):
                    names = [t.id for t in p.targets if isinstance(t, ast.Name)]
                for i in ifs:
                    if any(isinstance(n, ast.Name) and n.id in names for n in ast.walk(i.test)):
                        handler = i
                        break
            if handler is None:
                unbound.append(where)
                continue
            key = next((k.value.value for k in call.keywords if k.arg == "key" and isinstance(k.value, ast.Constant)), None)
            if key in UI_ONLY_BUTTONS:       # yalnız yerel taslağı atar (kayıt yok) — dinamik testte ayrıca doğrulanır
                continue
            if not _is_real(_calls_in(handler.body)):
                dead.append(f"{where} işleyici gövdesi servis çağırmıyor: {_calls_in(handler.body)}")
    assert total >= 20, total       # beklenen düğme sayısı (kontrol: avlanan düğme sayısı)
    assert unbound == [], unbound
    assert dead == [], dead


# ----------------------------------------------------------------------------------------- (b) dinamik
@pytest.fixture()
def app(tmp_path, monkeypatch):
    configure_database(f"sqlite:///{tmp_path / 'ui.db'}")
    monkeypatch.setattr(fstate, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    at = AppTest.from_file(APP, default_timeout=180)
    at.run()
    return at


def _btn(at, key=None, label=None):
    return next(b for b in at.button if (key is not None and b.key == key) or (label is not None and b.label == label))


def _demo(at):
    _btn(at, "top_demo").click().run()
    assert not at.exception
    return at


def _page(at, name):
    at.sidebar.radio[0].set_value(name).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_ten_pages_render_without_exception_or_error(app):
    _demo(app)
    app.session_state["project_id"] = "demo-project"
    # simülasyon + optimizasyon olmadan
    for page in PAGES:
        _page(app, page)
        assert not app.error, (page, [e.value for e in app.error])
    assert len(PAGES) == 11 and "Digital Twin" in PAGES   # master prompt'taki 10 sayfa + Digital Twin
    _btn(app, "top_sim").click().run()
    for page in PAGES:                   # simülasyon varken de
        _page(app, page)
        assert not app.error, (page, [e.value for e in app.error])


def test_top_demo_and_settings_demo_load_real_project(app):
    _demo(app)
    assert [p["id"] for p in services.list_projects()] == ["demo-project"]
    _page(app, "Settings")
    _btn(app, "settings_demo").click().run()
    assert not app.exception and len(services.list_projects()) == 1       # idempotent, çift yok


def test_run_simulation_button_persists_real_run(app):
    _demo(app)
    _btn(app, "top_sim").click().run()
    runs = services.list_simulation_runs("demo-project")
    assert len(runs) == 1 and runs[0]["seed"] == 42 and runs[0]["throughput_per_hour"] == pytest.approx(7.75)
    assert any("verim 7.75" in s.value for s in app.success)


def test_sim_form_uses_form_parameters(app):
    _demo(app)
    _page(app, "Simulation")
    app.number_input(key="sim_seed").set_value(5)
    app.number_input(key="sim_dur").set_value(2.0)
    _btn(app, label="Simülasyonu çalıştır").click().run()
    assert not app.exception and not app.error
    run = services.list_simulation_runs("demo-project")[-1]
    assert run["seed"] == 5
    sim = services.get_latest_sim("demo-project")
    assert sim.config.duration_s == 7200 and sim.seed == 5


def test_suggested_actions_buttons(app):
    _demo(app)
    _btn(app, "sugg_sim").click().run()
    assert len(services.list_simulation_runs("demo-project")) == 1
    assert not any(b.key == "sugg_sim" for b in app.button)                # sonuç var -> öneri kalkar
    _btn(app, "sugg_opt").click().run()
    assert not app.exception and not app.error
    latest = services.get_latest_optimization("demo-project")
    assert latest is not None and len(latest[1].alternatives) >= 3


def test_optimize_form_apply_and_scenario_pages(app):
    _demo(app)
    _page(app, "Optimization")
    app.number_input(key="opt_iter").set_value(40)
    app.checkbox(key="opt_sim").uncheck()
    app.number_input(key="opt_target").set_value(99.0)
    _btn(app, label="Optimizasyonu çalıştır").click().run()
    assert not app.exception
    opt_id, res = services.get_latest_optimization("demo-project")
    assert res.config.iterations == 40 and res.target_met is False and res.config.target_distance_reduction_pct == 99.0
    # %99 hedef sağlanamadı: UI bunu st.error ile DÜRÜSTÇE gösterir (başarı gibi göstermez)
    assert [e.value for e in app.error] == ["Hedef sağlanamadı."]
    n_before = len(services.list_layout_versions("demo-project"))
    apply_btn = _btn(app, "opt_apply")
    assert not apply_btn.disabled
    apply_btn.click().run()
    assert not app.exception
    assert len(services.list_layout_versions("demo-project")) == n_before + 1


def test_export_report_button_creates_files_and_rows(app, tmp_path):
    _demo(app)
    _btn(app, "top_sim").click().run()
    _btn(app, "top_export").click().run()
    assert not app.exception and not app.error
    rows = services.list_reports("demo-project")
    kinds = sorted({r["kind"] for r in rows})
    assert {"html", "pdf", "xlsx", "png"} <= set(kinds), kinds
    for r in rows:
        assert Path(r["path"]).exists() and Path(r["path"]).stat().st_size > 1000, r
    assert any("optimizasyon sonucu yok" in s.value for s in app.success)   # dürüst bildirim
    _page(app, "Reports")
    assert len(app.get("download_button")) >= 4                            # indirme düğmeleri gerçek dosyalarla


def test_reports_page_generate_button(app):
    _demo(app)
    _btn(app, "top_sim").click().run()
    _page(app, "Reports")
    app.multiselect(key="rep_fmt").set_value(["HTML"])
    app.text_area(key="rep_ai").input("Darboğaz drill.")
    _btn(app, "rep_go").click().run()
    assert not app.exception and not app.error
    html_rows = [r for r in services.list_reports("demo-project") if r["kind"] == "html"]
    assert len(html_rows) == 1
    assert "Darboğaz drill." in Path(html_rows[0]["path"]).read_text(encoding="utf-8")


def test_report_without_simulation_is_refused_visibly(app):
    _demo(app)
    _btn(app, "top_export").click().run()
    assert any("simülasyon" in e.value.lower() for e in app.error)
    assert services.list_reports("demo-project") == []


def test_ai_panel_offline_buttons_and_form(app):
    _demo(app)
    _btn(app, "ai_offline").click().run()
    assert not app.exception
    chat = app.session_state["chat"]
    assert chat[0] == ("user", "Bu tesisi analiz et")
    assert "material_flow_routes: D_total=1540.0 m/sa" in chat[1][1]
    # sohbet formu: anahtar yok -> offline agent
    app.text_area(key="ai_msg").input("Bu tesisi analiz et")
    _btn(app, label="Gönder").click().run()
    assert not app.exception
    last = app.session_state["chat"][-1][1]
    assert "AI asistanı devre dışı" in last and "1540.0 m/saat" in last
    # AI Engineer sayfası düğmesi
    _page(app, "AI Engineer")
    n = len(app.session_state["chat"])
    _btn(app, "ai_page_offline").click().run()
    assert not app.exception and len(app.session_state["chat"]) == n + 1


def test_layout_designer_add_move_delete_save_and_discard(app):
    _demo(app)
    _page(app, "Layout Designer")
    # geçerli yeni istasyon ekle (formda doldur)
    app.text_input(key="add_id").input("newst")
    app.text_input(key="add_name").input("Yeni İstasyon")
    app.number_input(key="add_x").set_value(13.6)
    app.number_input(key="add_y").set_value(2.0)
    app.number_input(key="add_w").set_value(2.8)
    _btn(app, label="Ekle").click().run()
    assert not app.exception
    draft = app.session_state["draft_layout"]
    assert "newst" in [w.id for w in draft.workstations]
    assert len(services.get_layout("demo-project").workstations) == 7            # kayıt değişmedi (taslak)
    _btn(app, "save_draft").click().run()
    assert not app.exception
    vs = services.list_layout_versions("demo-project")
    assert len(vs) == 2
    assert len(services.get_layout("demo-project", vs[1]["id"]).workstations) == 8     # yeni sürüm DB'de var
    # (F-06 geçici çözümü: kullanıcı senaryo seçiciden yeni sürümü seçer)
    [b for b in app.selectbox if b.key.startswith("scenario_demo-project")][0].set_value(vs[1]["id"]).run()
    assert [v["active"] for v in services.list_layout_versions("demo-project")] == [False, True]
    # taşı -> taslağı at
    app.selectbox(key="mv_sid").set_value("raw").run()
    app.number_input(key="mv_y_raw").set_value(2.3).run()
    _btn(app, "mv_apply").click().run()
    assert app.session_state["draft_layout"].station("raw").y == 2.3
    _btn(app, "reset_draft").click().run()
    assert app.session_state["draft_layout"].station("raw").y == 2.0
    # sil: rotada kullanılan istasyon silinemez (düğme yok); kullanılmayan silinebilir
    app.selectbox(key="del_sid").set_value("newst").run()
    _btn(app, "del_apply").click().run()
    assert "newst" not in [w.id for w in app.session_state["draft_layout"].workstations]


def test_layout_designer_invalid_draft_blocked_unless_forced(app):
    """Geçersiz (çakışan) taslak 'yine de kaydet' işaretsiz KAYDEDİLMEZ; işaretliyse kaydedilir (kullanıcı kararı)."""
    _demo(app)
    _page(app, "Layout Designer")
    app.selectbox(key="mv_sid").set_value("raw").run()
    app.number_input(key="mv_x_raw").set_value(12.0).run()
    app.number_input(key="mv_y_raw").set_value(14.0).run()
    _btn(app, "mv_apply").click().run()
    assert any("GEÇERSİZ" in e.value for e in app.error)
    _btn(app, "save_draft").click().run()
    assert len(services.list_layout_versions("demo-project")) == 1
    assert any("Layout geçersiz" in e.value for e in app.error)
    app.checkbox(key="save_invalid").check().run()
    _btn(app, "save_draft").click().run()
    assert len(services.list_layout_versions("demo-project")) == 2          # kullanıcı açıkça zorladı


def test_scenario_switch_and_comparison_page(app):
    _demo(app)
    _btn(app, "top_sim").click().run()
    # ikinci sim farklı seed ile (Process Flow formu)
    _page(app, "Simulation")
    app.number_input(key="sim_seed").set_value(7)
    _btn(app, label="Simülasyonu çalıştır").click().run()
    assert len(services.list_simulation_runs("demo-project")) == 2
    _page(app, "Scenario Comparison")
    assert not app.error
    assert any("CRN" in w.value for w in app.warning) or any("CRN" in s.value for s in app.success)
    assert any("HAYIR" in w.value for w in app.warning)                     # seed farklı -> dürüst uyarı


def test_saved_draft_stays_active_after_rerun(app):
    _demo(app)
    _page(app, "Layout Designer")
    app.text_input(key="add_id").input("newst")
    app.text_input(key="add_name").input("Yeni")
    app.number_input(key="add_x").set_value(13.6)
    app.number_input(key="add_y").set_value(2.0)
    app.number_input(key="add_w").set_value(2.8)
    _btn(app, label="Ekle").click().run()
    _btn(app, "save_draft").click().run()
    vs = services.list_layout_versions("demo-project")
    assert len(vs) == 2 and vs[1]["active"] is True                   # yeni kaydedilen sürüm aktif kalmalı
    assert len(services.get_layout("demo-project").workstations) == 8


def test_applied_optimization_alternative_stays_active(app):
    _demo(app)
    _page(app, "Optimization")
    app.number_input(key="opt_iter").set_value(30)
    app.checkbox(key="opt_sim").uncheck()
    _btn(app, label="Optimizasyonu çalıştır").click().run()
    _btn(app, "opt_apply").click().run()
    vs = services.list_layout_versions("demo-project")
    assert len(vs) == 2 and vs[1]["active"] is True


def _agent_with_pending(tmp_path):
    from backend.agent.orchestrator import FactoryFlowAgent
    from backend.agent.tool_executor import ToolExecutor
    from backend.agent.tool_registry import ToolContext
    from backend.config import Settings
    from tests.fakes import FakeGeminiClient, Step

    client = FakeGeminiClient([Step.call("update_workstation", {"station_id": "raw", "y": 2.3}),
                               Step.text("Öneri hazır; onayınızı bekliyorum.")])
    return FactoryFlowAgent(client, ToolExecutor(ToolContext(artifact_dir=tmp_path / "art")),
                            settings=Settings(external_data_consent=True))


def test_ai_panel_approve_and_reject_buttons_work_end_to_end(app, tmp_path):
    from tests.fakes import Step

    _demo(app)
    app.session_state["_agent"] = _agent_with_pending(tmp_path)
    app.text_area(key="ai_msg").input("raw istasyonunu 0.3 m yukarı taşı")
    _btn(app, label="Gönder").click().run()
    assert not app.exception
    assert len(services.list_layout_versions("demo-project")) == 1                  # onaysız: değişmedi
    appr = [b for b in app.button if b.key and b.key.startswith("appr_")]
    rej = [b for b in app.button if b.key and b.key.startswith("rej_")]
    assert len(appr) == 1 and len(rej) == 1
    appr[0].click().run()
    assert not app.exception
    vs = services.list_layout_versions("demo-project")
    assert len(vs) == 2                                                            # onaydan sonra yeni sürüm
    assert services.get_layout("demo-project", vs[1]["id"]).station("raw").y == 2.3
    # ikinci öneri -> reddet -> değişmez
    app.session_state["_agent"].client.script.extend([
        Step.call("update_workstation", {"station_id": "cut", "y": 14.3}), Step.text("Hazır.")])
    app.text_area(key="ai_msg").input("cut istasyonunu taşı")
    _btn(app, label="Gönder").click().run()
    rej = [b for b in app.button if b.key and b.key.startswith("rej_")]
    assert len(rej) == 1
    rej[0].click().run()
    assert len(services.list_layout_versions("demo-project")) == 2
    assert "reddedildi" in app.session_state["chat"][-1][1]
