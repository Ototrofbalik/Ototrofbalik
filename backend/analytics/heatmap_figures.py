"""[Master sözleşmesi — DONDURULDU] Isı haritası Plotly figürleri. Uygulayan: B ajanı."""
from __future__ import annotations

import plotly.graph_objects as go

from backend.analytics.heatmaps import FromToMatrix, TimeGrid


def time_grid_figure(grid: TimeGrid, title: str = "", zmax: float | None = None) -> go.Figure:
    """İstasyon × zaman ısı haritası. zmax verilirse renk ölçeği sabitlenir (önce/sonra karşılaştırma)."""
    raise NotImplementedError


def time_grid_compare_figure(before: TimeGrid, after: TimeGrid, labels: tuple[str, str] = ("Önce", "Sonra")) -> go.Figure:
    """İki TimeGrid yan yana, ORTAK renk ölçeği."""
    raise NotImplementedError


def from_to_figure(m: FromToMatrix, value: str = "moves", title: str = "") -> go.Figure:
    """value: 'moves' (hareket/sa) | 'metres' (m/sa) | 'distance' (m). Boş hücreler boş kalır."""
    raise NotImplementedError
