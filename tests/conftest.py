"""Testleri geliştiricinin yerel .env dosyasından yalıt (gerçek API anahtarı testleri etkilemesin).

Canlı Gemini testi (RUN_GEMINI_LIVE=1) .env'i bilerek kullanır; o durumda yalıtım yapılmaz.
"""
from __future__ import annotations

import os

import pytest

from backend.config import Settings, get_settings


@pytest.fixture(autouse=True, scope="session")
def _isolate_dotenv():
    if os.environ.get("RUN_GEMINI_LIVE") == "1":
        yield
        return
    old = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    yield
    Settings.model_config["env_file"] = old
    get_settings.cache_clear()
