"""[Layout&Routing Engineer] Layout doğrulama."""
from backend.schemas import Layout, ValidationReport


def validate_layout(layout: Layout) -> ValidationReport:
    """Kontroller: sınırlar, istasyon çakışması, engel çakışması, güvenlik boşluğu (facility.safety_gap_m),
    koridor min genişliği, koridorların engelden geçmemesi, her istasyonun koridora erişimi
    (station_access_max_m). Hataları ValidationIssue olarak döner; error yoksa valid=True."""
    raise NotImplementedError
