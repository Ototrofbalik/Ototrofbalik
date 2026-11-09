"""Proje paketi: tüm modüllerin ortak girdisi."""
from __future__ import annotations

from pydantic import Field

from .common import Strict
from .flow import FlowEdge, Product
from .layout import Layout
from .simulation import CostConfig, SimConfig


class Project(Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")   # dosya yolu / URL için güvenli
    name: str
    description: str = ""
    is_demo: bool = False
    layout: Layout
    products: list[Product]
    flow_edges: list[FlowEdge] = Field(default_factory=list)  # boşsa products rotalarından türetilir
    sim_config: SimConfig
    cost: CostConfig | None = None
    data_label: str = ""     # örn. "ÖRNEK / VARSAYIMSAL VERİ"
