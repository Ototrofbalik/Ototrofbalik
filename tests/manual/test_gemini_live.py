"""Gerçek Gemini bağlantı testi. VARSAYILAN ATLANIR; yalnız RUN_GEMINI_LIVE=1 ve geçerli .env ile çalışır.

Çalıştırma: RUN_GEMINI_LIVE=1 pytest tests/manual/test_gemini_live.py -s
Kota harcar; ücretsiz kota garantisi yoktur.
"""
from __future__ import annotations

import os

import pytest

from backend.agent.gemini_client import ChatTurn, GeminiClient
from backend.config import get_settings
from backend.schemas import GeminiStatus

pytestmark = pytest.mark.skipif(os.environ.get("RUN_GEMINI_LIVE") != "1",
                                reason="Canlı Gemini testi: RUN_GEMINI_LIVE=1 ile açın.")


def test_live_connection_and_model():
    if not get_settings().gemini_active:
        pytest.skip("GEMINI_API_KEY / GEMINI_ENABLED yapılandırılmamış.")
    client = GeminiClient()
    assert client.test_connection() == GeminiStatus.CONNECTED
    models = client.list_available_models()
    print("Kullanılabilir modeller:", [m["name"] for m in models][:10])
    resp = client.generate([ChatTurn(role="user", text="Sadece 'tamam' yaz.")], system_prompt="Kısa yanıt ver.",
                           tools=[])
    assert resp.text
