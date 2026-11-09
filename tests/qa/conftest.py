"""[QA] Ortak fixture'lar. Pahalı hesaplamalar (optimizasyon, rapor dışa aktarma) oturum başına BİR kez çalışır.

Geliştirici ajanların test yardımcıları kullanılmaz; yalnız üretim API'leri ve tests/fakes/FakeGeminiClient.
Gerçek Gemini/ağ yok. Ortamda GEMINI_API_KEY bulunmaz (autouse fixture siler).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.config import reset_settings_cache
from backend.database import configure_database
from backend.demo.demo_factory import build_demo_project
from backend.pipeline import FacilityAnalysis, analyze_facility
from backend.reporting import builder

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_gemini_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def db(tmp_path):
    """Test başına temiz SQLite DB (dosya) ve demo proje kaydı."""
    configure_database(f"sqlite:///{tmp_path / 'qa.db'}")
    from backend import services

    services.ensure_demo_project()
    return tmp_path / "qa.db"


@pytest.fixture(scope="session")
def demo_project():
    return build_demo_project()


@pytest.fixture(scope="session")
def analysis(demo_project, tmp_path_factory) -> FacilityAnalysis:
    """analyze_facility(demo, optimize=True) — oturum başına tek koşu (~15 sn)."""
    out = tmp_path_factory.mktemp("pipeline_artifacts")
    return analyze_facility(demo_project, optimize=True, out_dir=out)


@pytest.fixture(scope="session")
def report_bundle(demo_project, analysis, tmp_path_factory):
    """Demo + optimizasyon için HTML/PDF/Excel dışa aktarımı (bir kez)."""
    out = tmp_path_factory.mktemp("report_files")
    ctx = builder.build_report_context(demo_project, analysis.baseline_sim, analysis.optimization)
    return {
        "ctx": ctx,
        "html": builder.export_html(ctx, out),
        "pdf": builder.export_pdf(ctx, out),
        "xlsx": builder.export_excel(ctx, out),
        "out": out,
    }
