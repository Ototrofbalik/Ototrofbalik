"""Uygulama ayarları (.env). Sırlar SecretStr'dir; asla loglanmaz/yazdırılmaz."""
from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    gemini_api_key: SecretStr | None = None
    gemini_model: str = ""
    gemini_enabled: bool = True
    gemini_max_requests_per_task: int = 10
    gemini_daily_request_budget: int = 100
    gemini_timeout_s: float = 60.0

    database_url: str = "sqlite:///./data/factoryflow.db"

    # Dış API'ye (Gemini vb.) veri gönderme için tek seferlik kullanıcı onayı
    external_data_consent: bool = False

    # Google Sheets / Drive (OAuth yoksa devre dışı)
    google_credentials_path: str | None = None
    google_integration_enabled: bool = False

    @property
    def gemini_configured(self) -> bool:
        key = self.gemini_api_key.get_secret_value() if self.gemini_api_key else ""
        return bool(key) and key != "YOUR_API_KEY_HERE"

    @property
    def gemini_active(self) -> bool:
        return self.gemini_enabled and self.gemini_configured


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
