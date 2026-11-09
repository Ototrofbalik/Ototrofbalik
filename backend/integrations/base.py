"""Google Sheets / Drive için soyut arayüz ve "bağlı değil" varsayılan uygulaması.

OAuth/kimlik bilgisi yokken uygulama çalışır; bu bağlayıcılar açık bir hata (IntegrationNotConnected) verir,
sahte veri döndürmez. Gerçek uygulamalar aynı arayüzü gerçekleştirip register_* ile kaydedilir.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class IntegrationNotConnected(RuntimeError):
    """Entegrasyon yapılandırılmadı / OAuth yok."""


class GoogleSheetsConnector(ABC):
    name = "google_sheets"

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    def read_range(self, spreadsheet_id: str, cell_range: str) -> list[list[Any]]: ...

    @abstractmethod
    def write_range(self, spreadsheet_id: str, cell_range: str, values: list[list[Any]]) -> None: ...


class GoogleDriveConnector(ABC):
    name = "google_drive"

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    def upload_file(self, local_path: str, folder_id: str | None = None) -> str:
        """Yüklenen dosyanın Drive kimliğini döner."""

    @abstractmethod
    def download_file(self, file_id: str, local_path: str) -> str: ...


_MSG = (
    "Google entegrasyonu bağlı değil (OAuth yapılandırılmamış). "
    "Dosyayı yerel olarak içe/dışa aktarabilirsiniz."
)


class NotConnectedSheets(GoogleSheetsConnector):
    @property
    def connected(self) -> bool:
        return False

    def read_range(self, spreadsheet_id: str, cell_range: str) -> list[list[Any]]:
        raise IntegrationNotConnected(_MSG)

    def write_range(self, spreadsheet_id: str, cell_range: str, values: list[list[Any]]) -> None:
        raise IntegrationNotConnected(_MSG)


class NotConnectedDrive(GoogleDriveConnector):
    @property
    def connected(self) -> bool:
        return False

    def upload_file(self, local_path: str, folder_id: str | None = None) -> str:
        raise IntegrationNotConnected(_MSG)

    def download_file(self, file_id: str, local_path: str) -> str:
        raise IntegrationNotConnected(_MSG)


_sheets: GoogleSheetsConnector = NotConnectedSheets()
_drive: GoogleDriveConnector = NotConnectedDrive()


def register_sheets(connector: GoogleSheetsConnector) -> None:
    global _sheets
    _sheets = connector


def register_drive(connector: GoogleDriveConnector) -> None:
    global _drive
    _drive = connector


def get_sheets() -> GoogleSheetsConnector:
    return _sheets


def get_drive() -> GoogleDriveConnector:
    return _drive


def integration_status() -> dict[str, Any]:
    return {
        "google_sheets": {"connected": _sheets.connected, "message": "" if _sheets.connected else _MSG},
        "google_drive": {"connected": _drive.connected, "message": "" if _drive.connected else _MSG},
    }
