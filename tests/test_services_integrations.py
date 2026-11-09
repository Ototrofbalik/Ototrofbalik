"""Ayarlar, Google entegrasyon arayüzü ve data/demo_factory dosyaları."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend import services
from backend.config import Settings
from backend.demo.demo_factory import build_demo_project
from backend.integrations import (
    IntegrationNotConnected,
    NotConnectedDrive,
    NotConnectedSheets,
    get_drive,
    get_sheets,
)
from backend.schemas import Project

DEMO_DIR = Path(__file__).resolve().parent.parent / "data" / "demo_factory"


def test_settings_secret_not_exposed(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-key")
    s = Settings(_env_file=None)
    assert s.gemini_configured and s.gemini_active
    assert "super-secret-key" not in repr(s) and "super-secret-key" not in str(s.model_dump())
    assert s.external_data_consent is False


def test_settings_placeholder_key_is_not_configured(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "YOUR_API_KEY_HERE")
    assert Settings(_env_file=None).gemini_configured is False
    monkeypatch.delenv("GEMINI_API_KEY")
    assert Settings(_env_file=None).gemini_active is False


def test_google_connectors_default_not_connected():
    assert isinstance(get_sheets(), NotConnectedSheets) and isinstance(get_drive(), NotConnectedDrive)
    with pytest.raises(IntegrationNotConnected):
        get_sheets().read_range("id", "A1:B2")
    with pytest.raises(IntegrationNotConnected):
        get_drive().upload_file("x.txt")


def test_demo_files_match_builder():
    project = Project.model_validate_json((DEMO_DIR / "demo_project.json").read_text(encoding="utf-8"))
    assert project == build_demo_project()
    csv_text = (DEMO_DIR / "workstations.csv").read_text(encoding="utf-8")
    assert csv_text == services.export_workstations_csv(build_demo_project().layout)
