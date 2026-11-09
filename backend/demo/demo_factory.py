"""Demo fabrika: 30x20 m, 7 istasyon, 2 operatör, 1 forklift. TÜM VERİ ÖRNEK / VARSAYIMSALDIR.

Yerleşim kasıtlı olarak zikzaklıdır (akış sırası ile fiziksel sıra uyumsuz) -> optimizasyon alanı vardır.
"""
from __future__ import annotations

from backend.schemas import (
    CostConfig, Corridor, Facility, FlowEdge, Layout, Obstacle, Point, Product, Project, Rect,
    ResourceConfig, RouteStep, SimConfig, StationKind, VehicleType, Workstation,
)

DEMO_LABEL = "ÖRNEK / VARSAYIMSAL VERİ — gerçek bir tesisi temsil etmez"
W, L = 4.0, 3.0  # istasyon ayak izi (m)


def _ws(id_, name, kind, x, y, proc, setup=0.0, cap=1, op=False) -> Workstation:
    return Workstation(
        id=id_, name=name, kind=kind, x=x, y=y, width=W, length=L,
        processing_time_s=proc, setup_time_s=setup, capacity=cap, requires_operator=op,
        input_offset=Point(x=-1.0, y=0.0), output_offset=Point(x=1.0, y=0.0),
    )


def build_demo_layout() -> Layout:
    stations = [
        _ws("raw", "Raw Material", StationKind.RAW_MATERIAL, 1, 2, 60),
        _ws("pack", "Packaging", StationKind.PACKAGING, 9, 2, 150, op=True),
        _ws("drill", "Drilling", StationKind.MACHINE, 17, 2, 300, setup=20),
        _ws("ship", "Shipping", StationKind.SHIPPING, 25, 2, 60),
        _ws("asm", "Assembly", StationKind.ASSEMBLY, 3, 14, 420, cap=2, op=True),
        _ws("qc", "Quality Control", StationKind.QUALITY, 12, 14, 180, op=True),
        _ws("cut", "Cutting", StationKind.MACHINE, 22, 14, 240, setup=15),
    ]
    corridors = [
        Corridor(id="main", points=[Point(x=1, y=10), Point(x=29, y=10)], width=3.0),
        Corridor(id="west", points=[Point(x=1, y=6), Point(x=1, y=10), Point(x=1, y=13)], width=2.0),
    ]
    obstacles = [Obstacle(id="col1", name="Kolon", rect=Rect(x=6.5, y=3.0, w=1.0, h=1.0), kind="column")]
    return Layout(
        id="demo-layout-v1", name="Demo Layout (mevcut durum)", version=1,
        facility=Facility(name="Demo Fabrika", width=30, height=20),
        workstations=stations, corridors=corridors, obstacles=obstacles,
    )


def build_demo_project() -> Project:
    route = [
        RouteStep(station_id="raw"),
        RouteStep(station_id="cut", transport_in=VehicleType.FORKLIFT),
        RouteStep(station_id="drill", transport_in=VehicleType.FORKLIFT),
        RouteStep(station_id="asm", transport_in=VehicleType.FORKLIFT),
        RouteStep(station_id="qc", transport_in=VehicleType.OPERATOR),
        RouteStep(station_id="pack", transport_in=VehicleType.OPERATOR),
        RouteStep(station_id="ship", transport_in=VehicleType.FORKLIFT),
    ]
    product = Product(id="widget-a", name="Widget-A (örnek)", route=route, arrival_rate_per_hour=11.0)
    sim = SimConfig(
        duration_s=8 * 3600, seed=42, replications=1,
        resources=[
            ResourceConfig(vehicle=VehicleType.OPERATOR, count=2, speed_mps=1.2, load_time_s=5, unload_time_s=5),
            ResourceConfig(vehicle=VehicleType.FORKLIFT, count=1, speed_mps=2.5, load_time_s=15, unload_time_s=15),
        ],
    )
    cost = CostConfig(cost_per_meter={VehicleType.OPERATOR: 0.05, VehicleType.FORKLIFT: 0.20}, assumed=True)
    return Project(
        id="demo-project", name="FactoryFlow Demo", description="30x20 m örnek üretim hattı", is_demo=True,
        layout=build_demo_layout(), products=[product], sim_config=sim, cost=cost, data_label=DEMO_LABEL,
    )
