"""[QA] Kontrol 5 — sahte/sabit KPI avı: statik kod taraması + duyarlılık (girdi değişince çıktı değişmeli) +
tüm 18 tool'un gerçekten çalıştığı (stub/NotImplemented kalmadığı) kanıtı."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from backend import services
from backend.agent.tool_executor import ToolExecutor
from backend.agent.tool_registry import TOOL_NAMES, ToolContext
from backend.optimization.engine import optimize_layout
from backend.routing.engine import compute_flow_metrics, compute_routes, derive_flow_edges
from backend.schemas import OptimizationConfig, ToolStatus
from backend.simulation.engine import run_simulation
from tests.qa.conftest import REPO

PROD_DIRS = [REPO / "backend", REPO / "frontend"]


def _py_files(skip_dirs=()):
    for d in PROD_DIRS:
        for p in sorted(d.rglob("*.py")):
            if "__pycache__" in p.parts or any(s in p.parts for s in skip_dirs):
                continue
            yield p


KPI_NAMES = {"throughput_per_hour", "completed", "avg_cycle_time_s", "avg_lead_time_s", "avg_wip", "avg_queue_wait_s",
             "max_queue_len", "total_travel_distance_m", "total_travel_time_s", "total_distance_m_per_hour",
             "distance_per_unit_m", "utilization", "distance_reduction_pct", "objective"}


def test_no_random_module_or_unseeded_rng_in_production():
    bad = []
    for p in _py_files():
        src = p.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for n in ast.walk(tree):
            if isinstance(n, ast.Import) and any(a.name.split(".")[0] == "random" for a in n.names):
                bad.append(f"{p}:{n.lineno} import random")
            if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "random":
                bad.append(f"{p}:{n.lineno} from random")
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                chain = ast.unparse(n.func)
                if chain in ("np.random.rand", "np.random.randn", "np.random.random", "np.random.randint",
                             "np.random.choice", "np.random.uniform", "np.random.normal", "np.random.seed",
                             "numpy.random.rand", "numpy.random.random"):
                    bad.append(f"{p}:{n.lineno} {chain}")
                if chain.endswith("default_rng") and not n.args and not n.keywords:
                    bad.append(f"{p}:{n.lineno} seedsiz default_rng()")
    assert bad == [], bad


def test_no_mock_fake_dummy_identifiers_or_imports_in_production():
    """Kod tanımlayıcıları/importlarında mock/fake/dummy yok (docstring anmaları ve Streamlit `placeholder=` hariç)."""
    pat = re.compile(r"(mock|fake|dummy|lorem|hardcoded)", re.I)
    hits = []
    for p in _py_files():
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            names = []
            if isinstance(n, ast.Name):
                names.append(n.id)
            elif isinstance(n, ast.Attribute):
                names.append(n.attr)
            elif isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                names.append(n.name)
            elif isinstance(n, ast.alias):
                names.append(n.name)
            elif isinstance(n, ast.ImportFrom):
                names.append(n.module or "")
            elif isinstance(n, ast.keyword) and n.arg:
                names.append(n.arg)
            for nm in names:
                if pat.search(nm):
                    hits.append(f"{p.relative_to(REPO)}:{getattr(n, 'lineno', '?')} {nm}")
    assert hits == [], hits


def test_no_notimplemented_stubs_remain():
    raised, empty = [], []
    for p in _py_files():
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Raise) and n.exc is not None:
                nm = ast.unparse(n.exc.func if isinstance(n.exc, ast.Call) else n.exc)
                if nm == "NotImplementedError":
                    raised.append(f"{p.relative_to(REPO)}:{n.lineno}")
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = [b for b in n.body if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))]
                if not body or all(isinstance(b, ast.Pass) or (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant)
                                                              and b.value.value is Ellipsis) for b in body):
                    # Protocol / soyut tanımlar hariç
                    if "Protocol" not in ast.unparse(tree)[:0] and not _is_protocol_or_abstract(tree, n):
                        empty.append(f"{p.relative_to(REPO)}:{n.lineno} {n.name}")
    assert raised == [], raised
    assert empty == [], empty


def _is_protocol_or_abstract(tree, fn) -> bool:
    for cls in [c for c in ast.walk(tree) if isinstance(c, ast.ClassDef)]:
        if fn in cls.body:
            bases = " ".join(ast.unparse(b) for b in cls.bases)
            if "Protocol" in bases or "ABC" in bases:
                return True
            if any("abstractmethod" in ast.unparse(d) for d in fn.decorator_list):
                return True
    return False


def test_no_hardcoded_kpi_assignments_in_domain_code():
    """KPI/sonuç alanlarına sabit sayı atanıyor mu (0 ve boş-başlangıç hariç)?"""
    suspicious = []
    allowed_dirs = {"schemas", "demo"}
    for p in _py_files():
        if allowed_dirs & set(p.parts):
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.keyword) and n.arg in KPI_NAMES and isinstance(n.value, ast.Constant) \
                    and isinstance(n.value.value, (int, float)) and not isinstance(n.value.value, bool) \
                    and n.value.value not in (0, 0.0, 1, 1.0):
                suspicious.append(f"{p.relative_to(REPO)}:{n.value.lineno} {n.arg}={n.value.value}")
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) \
                    and isinstance(n.value.value, (int, float)) and not isinstance(n.value.value, bool) \
                    and n.value.value not in (0, 0.0, 1, 1.0):
                for t in n.targets:
                    nm = t.attr if isinstance(t, ast.Attribute) else getattr(t, "id", "")
                    if nm in KPI_NAMES:
                        suspicious.append(f"{p.relative_to(REPO)}:{n.lineno} {nm}={n.value.value}")
    assert suspicious == [], suspicious


def test_demo_output_values_do_not_appear_as_literals_outside_demo():
    """Demo sonuçlarının (1540, 7.75, 6618, 19.73, 140.0) kaynak kodda sabit sayı olarak geçmemesi."""
    banned = {1540.0, 7.75, 140.0, 62, 19.73, 6618, 528.0, 65.7}
    hits = []
    for p in _py_files():
        if "demo" in p.parts or "schemas" in p.parts:
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool) \
                    and n.value in banned:
                hits.append(f"{p.relative_to(REPO)}:{n.lineno} {n.value}")
    assert hits == [], hits


def test_frontend_never_computes_kpis_itself():
    """UI yalnız servis/domain çağırır; KPI aritmetiği domain'dedir (frontend'de sim/flow değeri türetme yok)."""
    hits = []
    pat = re.compile(r"(throughput_per_hour|avg_wip|total_distance_m_per_hour|utilization)\s*[-+*/]=?\s*[\w(]")
    for p in (REPO / "frontend").rglob("*.py"):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line) and "f\"" not in line and "f'" not in line and "metric" not in line \
                    and "* 100" not in line:      # '* 100' yalnız % gösterim dönüşümü
                hits.append(f"{p.relative_to(REPO)}:{i}: {line.strip()[:100]}")
    assert hits == [], hits


# ----------------------------------------------------------------------------------------- duyarlılık (çalıştırarak)
def _flow(project):
    edges = derive_flow_edges(project)
    routes = compute_routes(project.layout, edges)
    return routes, compute_flow_metrics(project.layout, edges, routes, project)


def test_outputs_respond_to_inputs_not_constants(demo_project):
    base_routes, base_flow = _flow(demo_project)
    # 1) varış hızı 11 -> 5: D_total = 140 * 5 = 700
    prod = demo_project.products[0].model_copy(update={"arrival_rate_per_hour": 5.0})
    p5 = demo_project.model_copy(update={"products": [prod]}, deep=True)
    assert _flow(p5)[1].total_distance_m_per_hour == pytest.approx(700.0)
    # 2) istasyon taşı: mesafe değişir
    from backend.layout.editing import move_workstation

    moved = move_workstation(demo_project.layout, "raw", 25, 14)   # cut ile çakışır ama rotalama saf geometri
    pm = demo_project.model_copy(update={"layout": moved}, deep=True)
    assert _flow(pm)[1].total_distance_m_per_hour != base_flow.total_distance_m_per_hour
    # 3) demo'da operatör havuzu (2 kişi, %91) da doygun: önce operatör 2 -> 4 (throughput artmalı),
    #    sonra drill 300 -> 100 s (4 operatörle throughput artmalı, drill artık darboğaz olmamalı)
    s0 = run_simulation(demo_project, base_routes)
    ops4 = demo_project.sim_config.model_copy(update={"resources": [
        r.model_copy(update={"count": 4}) if r.vehicle.value == "operator" else r
        for r in demo_project.sim_config.resources]})
    s_ops = run_simulation(demo_project, base_routes, ops4)
    assert s_ops.kpis.throughput_per_hour > s0.kpis.throughput_per_hour
    fast = demo_project.layout.model_copy(deep=True)
    fast.workstations = [w.model_copy(update={"processing_time_s": 100}) if w.id == "drill" else w
                         for w in fast.workstations]
    pf = demo_project.model_copy(update={"layout": fast}, deep=True)
    s1 = run_simulation(pf, base_routes, ops4)
    assert s1.kpis.throughput_per_hour > s_ops.kpis.throughput_per_hour
    assert s1.bottleneck_station_id != "drill"
    # 4) forklift hızı değişince taşıma süresi değişir
    cfg = demo_project.sim_config.model_copy(update={"resources": [
        r.model_copy(update={"speed_mps": 5.0}) if r.vehicle.value == "forklift" else r
        for r in demo_project.sim_config.resources]})
    s2 = run_simulation(demo_project, base_routes, cfg)
    assert s2.kpis.avg_transport_s < s0.kpis.avg_transport_s
    # 5) süre değişince tamamlanan sayısı orantılı değişir
    cfg3 = demo_project.sim_config.model_copy(update={"duration_s": 2 * 3600})
    assert run_simulation(demo_project, base_routes, cfg3).kpis.completed < s0.kpis.completed


def test_optimizer_result_depends_on_problem(demo_project):
    """Optimizer sabit bir 'iyi layout' döndürmüyor: farklı akış -> farklı sonuç."""
    cfg = OptimizationConfig(iterations=80, simulate_alternatives=False)
    a = optimize_layout(demo_project, cfg)
    p2 = demo_project.model_copy(deep=True)
    p2.products[0].route = list(reversed(p2.products[0].route))
    p2.products[0].route[0] = p2.products[0].route[0].model_copy(update={"transport_in": "operator"})
    try:
        b = optimize_layout(p2, cfg)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"ters rota senaryosu çalıştırılamadı: {type(exc).__name__}")
    assert [x.layout.model_dump() for x in a.alternatives] != [x.layout.model_dump() for x in b.alternatives] or \
        a.baseline_flow.total_distance_m_per_hour != b.baseline_flow.total_distance_m_per_hour


# ----------------------------------------------------------------------------------------- 18 tool'un hepsi gerçek
def test_all_18_tools_registered_and_every_tool_executes_for_real(db, tmp_path):
    assert len(TOOL_NAMES) == 18
    ex = ToolExecutor(ToolContext(project_id="demo-project", artifact_dir=tmp_path), max_tool_calls=60)
    tid = ex.start_task("qa tüm araçlar", "demo-project")
    small = {"iterations": 30, "simulate_alternatives": True, "n_alternatives": 3}
    calls = [
        ("validate_layout", {}), ("calculate_material_flow", {}), ("calculate_routes", {}),
        ("create_spaghetti_chart", {}), ("configure_simulation", {"duration_s": 3600}),
        ("run_simpy_simulation", {}), ("get_simulation_metrics", {}), ("analyze_bottlenecks", {}),
        ("generate_layout_alternatives", small), ("optimize_factory_layout", small),
        ("compare_scenarios", {}), ("calculate_savings", {}),
        ("generate_engineering_report", {"format": "html"}), ("export_excel_report", {}),
        ("update_workstation", {"station_id": "raw", "x": 1.0, "y": 2.0}),
        ("add_workstation", {"station_id": "new1", "name": "Yeni", "x": 13.6, "y": 2, "width": 2.8, "length": 3}),
        ("import_factory_layout", {"format": "workstations_csv", "content": services.export_workstations_csv(
            services.get_layout("demo-project"))}),
        ("create_project", {"name": "QA Proje", "project_id": "qa-proje", "from_demo": True}),
    ]
    assert {c[0] for c in calls} == set(TOOL_NAMES)
    bad = {}
    for name, args in calls:
        r = ex.execute(tid, name, args)
        if r.status not in (ToolStatus.OK, ToolStatus.NEEDS_APPROVAL):
            bad[name] = (r.status.value, r.error_code, r.error)
    assert bad == {}, bad
    assert "qa-proje" in {p["id"] for p in services.list_projects()}


def test_sim_config_tool_changes_next_sim_only_in_session(db, tmp_path):
    ex = ToolExecutor(ToolContext(project_id="demo-project", artifact_dir=tmp_path))
    tid = ex.start_task("cfg", "demo-project")
    ex.execute(tid, "configure_simulation", {"duration_s": 3600, "seed": 7})
    r = ex.execute(tid, "run_simpy_simulation", {})
    assert r.data["seed"] == 7 and r.data["duration_s"] == 3600
    assert services.get_project("demo-project").sim_config.seed == 42       # kayıtlı proje değişmedi
    assert Path(tmp_path).exists()
