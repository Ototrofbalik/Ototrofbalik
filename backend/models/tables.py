"""SQLAlchemy 2.0 tabloları (master prompt §14: 15 tablo).

Kurallar: layout geometrisi hem normalize satırlar hem de JSON blob olarak tutulur; layout'lar sürümlenir
(parent_id) ve ESKİ sürümler/simülasyonlar/optimizasyonlar asla silinmez.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database.base import Base


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class ProjectRow(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text, default="")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    data_label: Mapped[str] = mapped_column(String(256), default="")
    sim_config: Mapped[dict[str, Any]] = mapped_column(JSON)
    cost: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    flow_edges: Mapped[list[Any]] = mapped_column(JSON, default=list)
    active_layout_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


class LayoutRow(Base):
    __tablename__ = "layouts"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(256))
    version: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)   # tam Layout JSON blob
    created_at: Mapped[datetime] = mapped_column(default=_now)
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_layout_project_version"),)


class FacilityRow(Base):
    __tablename__ = "facilities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    layout_id: Mapped[str] = mapped_column(ForeignKey("layouts.id"), unique=True)
    name: Mapped[str] = mapped_column(String(256))
    width: Mapped[float] = mapped_column(Float)
    height: Mapped[float] = mapped_column(Float)
    grid: Mapped[float] = mapped_column(Float)
    safety_gap_m: Mapped[float] = mapped_column(Float)
    min_corridor_width_m: Mapped[float] = mapped_column(Float)
    station_access_max_m: Mapped[float] = mapped_column(Float)


class WorkstationRow(Base):
    __tablename__ = "workstations"
    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    layout_id: Mapped[str] = mapped_column(ForeignKey("layouts.id"), index=True)
    station_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(256))
    kind: Mapped[str] = mapped_column(String(32))
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    width: Mapped[float] = mapped_column(Float)
    length: Mapped[float] = mapped_column(Float)
    rotation: Mapped[str] = mapped_column(String(8))
    fixed: Mapped[bool] = mapped_column(Boolean, default=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("layout_id", "station_id", name="uq_ws_layout_station"),)


class CorridorRow(Base):
    __tablename__ = "corridors"
    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    layout_id: Mapped[str] = mapped_column(ForeignKey("layouts.id"), index=True)
    corridor_id: Mapped[str] = mapped_column(String(128))
    width: Mapped[float] = mapped_column(Float)
    one_way: Mapped[bool] = mapped_column(Boolean, default=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class ObstacleRow(Base):
    __tablename__ = "obstacles"
    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    layout_id: Mapped[str] = mapped_column(ForeignKey("layouts.id"), index=True)
    obstacle_id: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class ProductRow(Base):
    __tablename__ = "products"
    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    product_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(256))
    arrival_rate_per_hour: Mapped[float] = mapped_column(Float)
    batch_size: Mapped[int] = mapped_column(Integer, default=1)
    max_wip: Mapped[int | None] = mapped_column(Integer, nullable=True)
    steps: Mapped[list[ProcessRouteRow]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="ProcessRouteRow.seq"
    )
    __table_args__ = (UniqueConstraint("project_id", "product_id", name="uq_product_project"),)


class ProcessRouteRow(Base):
    __tablename__ = "process_routes"
    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_pk: Mapped[int] = mapped_column(ForeignKey("products.pk"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    station_id: Mapped[str] = mapped_column(String(128))
    transport_in: Mapped[str] = mapped_column(String(16))
    processing_time_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    setup_time_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    product: Mapped[ProductRow] = relationship(back_populates="steps")


class SimulationScenarioRow(Base):
    __tablename__ = "simulation_scenarios"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    layout_id: Mapped[str] = mapped_column(ForeignKey("layouts.id"), index=True)
    name: Mapped[str] = mapped_column(String(256))
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class SimulationRunRow(Base):
    __tablename__ = "simulation_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    layout_id: Mapped[str] = mapped_column(ForeignKey("layouts.id"), index=True)
    scenario_id: Mapped[int | None] = mapped_column(ForeignKey("simulation_scenarios.id"), nullable=True)
    seed: Mapped[int] = mapped_column(Integer)
    replications: Mapped[int] = mapped_column(Integer, default=1)
    result: Mapped[dict[str, Any]] = mapped_column(JSON)   # SimResult (events hariç; olaylar movement_events'te)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class MovementEventRow(Base):
    __tablename__ = "movement_events"
    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("simulation_runs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    t: Mapped[float] = mapped_column(Float)
    type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(128))
    product_id: Mapped[str] = mapped_column(String(128))
    station_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    from_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    to_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    vehicle: Mapped[str | None] = mapped_column(String(16), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    queue_len: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SimulationMetricRow(Base):
    __tablename__ = "simulation_metrics"
    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("simulation_runs.id"), index=True)
    scope: Mapped[str] = mapped_column(String(16))     # kpi | station | resource
    subject: Mapped[str] = mapped_column(String(128))  # "" | station_id | vehicle
    name: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)


class OptimizationResultRow(Base):
    __tablename__ = "optimization_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    baseline_layout_id: Mapped[str] = mapped_column(String(128))
    best_alternative_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    algorithm: Mapped[str] = mapped_column(String(64))
    target_met: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class AgentTaskRow(Base):
    __tablename__ = "agent_tasks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    user_text: Mapped[str] = mapped_column(Text, default="")
    reply_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="running")
    tool_calls: Mapped[list[Any]] = mapped_column(JSON, default=list)
    requests_used: Mapped[int] = mapped_column(Integer, default=0)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


class ReportRow(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))      # html | pdf | xlsx | png
    title: Mapped[str] = mapped_column(String(256), default="")
    path: Mapped[str] = mapped_column(String(1024))
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=_now)
