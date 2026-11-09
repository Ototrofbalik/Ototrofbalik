"""[Layout&Routing Engineer] Spaghetti Chart (Plotly)."""
import plotly.graph_objects as go

from backend.schemas import FlowMetrics, Layout, RouteSet, VehicleType


def spaghetti_figure(layout: Layout, routes: RouteSet, metrics: FlowMetrics,
                     vehicles: set[VehicleType] | None = None, title: str = "") -> go.Figure:
    """Layout + istasyonlar + engeller + yönlü rota çizgileri (araç rengi, frekansla kalınlık, ok)."""
    raise NotImplementedError


def heatmap_figure(layout: Layout, routes: RouteSet, metrics: FlowMetrics) -> go.Figure:
    raise NotImplementedError


def before_after_figure(before: tuple[Layout, RouteSet, FlowMetrics], after: tuple[Layout, RouteSet, FlowMetrics]) -> go.Figure:
    raise NotImplementedError


def layout_figure(layout: Layout, issues=None) -> go.Figure:
    """Yalnız layout görünümü; çakışmalar kırmızı."""
    raise NotImplementedError
