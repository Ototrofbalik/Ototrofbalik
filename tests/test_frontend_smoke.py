"""Streamlit AppTest smoke: tüm sayfalar hatasız yüklenir; düğmeler gerçek servislere bağlıdır."""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from backend.database.session import configure_database
from frontend import state as fstate
from frontend.state import PAGES

APP = str(Path(__file__).resolve().parent.parent / "frontend" / "streamlit_app.py")


@pytest.fixture()
def app(tmp_path, monkeypatch):
    configure_database("sqlite:///:memory:")
    monkeypatch.setattr(fstate, "REPORT_DIR", tmp_path / "reports")
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    return at


def _btn(at, key):
    return next(b for b in at.button if b.key == key)


def test_empty_state_loads_without_exception(app):
    assert not app.exception
    assert any(b.key == "top_demo" for b in app.button)


def test_all_pages_load_with_demo(app):
    _btn(app, "top_demo").click().run()
    assert not app.exception
    assert app.session_state["project_id"] == "demo-project"
    for page in PAGES:
        app.sidebar.radio[0].set_value(page).run()
        assert not app.exception, f"{page}: {[e.value for e in app.exception]}"


def test_run_simulation_and_report_buttons(app):
    _btn(app, "top_demo").click().run()
    _btn(app, "top_sim").click().run()
    assert not app.exception and not app.error
    assert any("Simülasyon tamamlandı" in s.value for s in app.success)
    _btn(app, "top_export").click().run()
    assert not app.exception and not app.error
    assert any("rapor dosyası üretildi" in s.value for s in app.success)


def test_optimize_error_is_visible_not_hidden(app):
    _btn(app, "top_demo").click().run()
    assert any(b.key == "top_opt" for b in app.button)
    _btn(app, "top_opt").click().run()
    assert not app.exception
    # Optimizasyon motoru stub ise açık hata; uygulanmışsa başarı mesajı — sahte sonuç yok.
    assert app.error or any("Optimizasyon tamamlandı" in s.value for s in app.success)


def test_layout_designer_edit_and_validation(app):
    _btn(app, "top_demo").click().run()
    app.sidebar.radio[0].set_value("Layout Designer").run()
    assert not app.exception
    # raw istasyonunu qc ile çakışacak yere taşı -> doğrulama hatası görünür
    app.selectbox(key="mv_sid").set_value("raw").run()
    app.number_input(key="mv_x_raw").set_value(12.0).run()
    app.number_input(key="mv_y_raw").set_value(14.0).run()
    _btn(app, "mv_apply").click().run()
    assert not app.exception
    assert any("GEÇERSİZ" in e.value for e in app.error)


def test_digital_twin_page_with_and_without_sim(app):
    _btn(app, "top_demo").click().run()
    app.sidebar.radio[0].set_value("Digital Twin").run()
    assert not app.exception
    assert app.info  # simülasyon yok -> yönlendirme
    _btn(app, "top_sim").click().run()
    app.sidebar.radio[0].set_value("Digital Twin").run()
    # veri modülü yoksa dürüst hata, varsa sayfa çökmeden render edilir
    assert not app.exception
