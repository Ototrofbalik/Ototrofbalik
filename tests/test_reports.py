"""Rapor motoru testleri: sayılar kaynak nesnelerle birebir eşleşir, dosyalar üretilir ve açılır.

Optimizasyon sonucu elle yazılmış sayılarla değil, gerçek compute_routes/run_simulation çıktılarıyla kurulur
(demo'da 'asm' istasyonu editing.move_workstation ile taşınır; geçerlilik validate_layout ile doğrulanır).
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from openpyxl import load_workbook

from backend.analytics.bottleneck import analyze_bottlenecks
from backend.demo.demo_factory import build_demo_project
from backend.layout import editing
from backend.layout.validation import validate_layout
from backend.reporting import builder
from backend.routing.engine import compute_flow_metrics, compute_routes, derive_flow_edges
from backend.schemas import (
    LayoutAlternative,
    OptimizationConfig,
    OptimizationResult,
    SimResult,
)
from backend.simulation.engine import run_simulation


def _eval(project, layout):
    edges = project.flow_edges or derive_flow_edges(project)
    routes = compute_routes(layout, edges)
    flow = compute_flow_metrics(layout, edges, routes, project)
    p2 = project.model_copy(update={"layout": layout}, deep=True)
    sim = run_simulation(p2, routes)
    return routes, flow, sim


@pytest.fixture(scope="module")
def project():
    return build_demo_project()


@pytest.fixture(scope="module")
def baseline(project):
    routes, flow, sim = _eval(project, project.layout)
    return {"routes": routes, "flow": flow, "sim": sim}


@pytest.fixture(scope="module")
def opt(project, baseline):
    alt_layout = editing.clone_layout(editing.move_workstation(project.layout, "asm", 17, 14), "alt-1", "Alt 1: asm taşındı")
    rep = validate_layout(alt_layout)
    assert rep.valid, rep.issues
    _, flow, sim = _eval(project, alt_layout)
    d0, d1 = baseline["flow"].total_distance_m_per_hour, flow.total_distance_m_per_hour
    assert d1 < d0
    alt = LayoutAlternative(
        id="alt-1", name="Alt 1", algorithm="manual_test", layout=alt_layout, validation=rep, objective=d1,
        flow=flow, sim=sim, distance_reduction_pct=(d0 - d1) / d0 * 100, moved_station_ids=["asm"])
    return OptimizationResult(
        baseline_layout_id=project.layout.id, baseline_flow=baseline["flow"], baseline_sim=baseline["sim"],
        alternatives=[alt], best_alternative_id="alt-1", rationale="Test: tek istasyon taşıma.",
        config=OptimizationConfig(algorithm="manual_test", seed=7))


def _table(ctx, key):
    for s in ctx["sections"]:
        for t in s["tables"]:
            if t["key"] == key:
                return t
    raise AssertionError(key)


def test_fourteen_sections_in_order(project, baseline, opt):
    ctx = builder.build_report_context(project, baseline["sim"], opt)
    assert [s["no"] for s in ctx["sections"]] == list(range(1, 15))
    assert [s["title"] for s in ctx["sections"]] == builder.SECTION_TITLES


def test_numbers_match_sources(project, baseline, opt):
    ctx = builder.build_report_context(project, baseline["sim"], opt)
    sim: SimResult = baseline["sim"]
    n = ctx["numbers"]
    assert n["d_total_m_per_hour"] == baseline["flow"].total_distance_m_per_hour
    assert n["throughput_per_hour"] == sim.kpis.throughput_per_hour
    assert n["seed"] == sim.seed == project.sim_config.seed
    kp = dict(_table(ctx, "kpis")["rows"])
    assert kp["throughput_per_hour"] == sim.kpis.throughput_per_hour
    assert kp["completed"] == sim.kpis.completed
    ft = _table(ctx, "from_to")["rows"]
    assert len(ft) == len(baseline["flow"].cells)
    assert sum(r[4] for r in ft) == pytest.approx(baseline["flow"].total_distance_m_per_hour)
    ev = dict(_table(ctx, "event_summary")["rows"])
    assert sum(ev.values()) == len(sim.events)
    st = {r[0]: r for r in _table(ctx, "station_stats")["rows"]}
    for s in sim.stations:
        assert st[s.station_id][1] == s.utilization
    bn = analyze_bottlenecks(sim)
    assert n["bottleneck_station_id"] == bn["bottleneck_station_id"]
    # önerilen alternatif + karşılaştırma
    best = opt.alternatives[0]
    assert n["best_d_total_m_per_hour"] == best.flow.total_distance_m_per_hour
    cmp_rows = {r[0]: r for r in _table(ctx, "comparison")["rows"]}
    assert cmp_rows["throughput_per_hour"][2] == sim.kpis.throughput_per_hour
    assert cmp_rows["throughput_per_hour"][3] == best.sim.kpis.throughput_per_hour
    assert cmp_rows["flow.total_distance_m_per_hour"][2] == baseline["flow"].total_distance_m_per_hour
    assert cmp_rows["flow.total_distance_m_per_hour"][3] == best.flow.total_distance_m_per_hour
    assert [r[0] for r in _table(ctx, "moved")["rows"]] == ["asm"]


def test_savings_from_cost_and_roi_not_computed(project, baseline, opt):
    ctx = builder.build_report_context(project, baseline["sim"], opt)
    sv = {r[0]: r[1] for r in _table(ctx, "savings")["rows"]}
    assert sv["tasarruf"] == pytest.approx(sv["baseline taşıma maliyeti"] - sv["senaryo taşıma maliyeti"])
    assert ctx["numbers"]["travel_cost_saving_per_hour"] == pytest.approx(sv["tasarruf"])
    text = " ".join(p for s in ctx["sections"] if s["id"] == "improvement_kpis" for p in s["paragraphs"])
    assert "ROI hesaplanamadı" in text


def test_no_cost_means_not_computed(project, baseline, opt):
    p = project.model_copy(update={"cost": None}, deep=True)
    ctx = builder.build_report_context(p, baseline["sim"], opt)
    text = " ".join(x for s in ctx["sections"] if s["id"] == "improvement_kpis" for x in s["paragraphs"])
    assert "Tasarruf ve ROI hesaplanamadı" in text
    assert "savings" not in {t["key"] for s in ctx["sections"] for t in s["tables"]}


def test_optional_optimization(project, baseline):
    ctx = builder.build_report_context(project, baseline["sim"], None)
    assert len(ctx["sections"]) == 14 and not ctx["has_optimization"]
    s9 = next(s for s in ctx["sections"] if s["id"] == "proposed_layout")
    assert "çalıştırılmadı" in s9["paragraphs"][0]
    assert "before_after" not in ctx["_figures"]
    s10 = next(s for s in ctx["sections"] if s["id"] == "scenario_comparison")
    assert "yapılamadı" in s10["paragraphs"][0]


def test_assumptions_and_data_label(project, baseline):
    ctx = builder.build_report_context(project, baseline["sim"], None)
    ass = next(s for s in ctx["sections"] if s["id"] == "assumptions")
    joined = " ".join(ass["bullets"])
    for item in baseline["sim"].not_modelled:
        assert item in joined
    assert project.data_label in joined
    assert ctx["data_label"] == project.data_label


def test_methodology_has_seed(project, baseline, opt):
    ctx = builder.build_report_context(project, baseline["sim"], opt)
    meth = dict(_table(ctx, "methodology")["rows"])
    assert meth["Baseline seed"] == baseline["sim"].seed
    assert meth["Optimizasyon seed"] == opt.config.seed


@pytest.fixture(scope="module")
def exported(project, baseline, opt, tmp_path_factory):
    out = tmp_path_factory.mktemp("rep")
    ctx = builder.build_report_context(project, baseline["sim"], opt, ai_text="Test AI yorumu: darboğaz hesaplandı.")
    return ctx, {
        "html": builder.export_html(ctx, out), "pdf": builder.export_pdf(ctx, out),
        "xlsx": builder.export_excel(ctx, out), "png": builder.export_png(ctx, out),
    }


def test_html_export(exported):
    ctx, files = exported
    html = files["html"].read_text(encoding="utf-8")
    assert html.count("<section") == 14
    assert html.count("data:image/png;base64,") >= 6
    assert ctx["data_label"] in html
    assert "Test AI yorumu" in html
    assert f"{ctx['numbers']['throughput_per_hour']:,.2f}" in html


def test_pdf_export(exported):
    _, files = exported
    data = files["pdf"].read_bytes()
    assert data.startswith(b"%PDF") and len(data) > 20_000
    import re
    assert len(re.findall(rb"/Type\s*/Page\b", data)) >= 3
    assert b"/Subtype /Image" in data or b"/Subtype/Image" in data


def test_excel_export(exported, baseline, opt):
    ctx, files = exported
    wb = load_workbook(files["xlsx"])
    assert {"KPI", "İstasyonlar", "From-To", "Olay Özeti", "Karşılaştırma"} <= set(wb.sheetnames)
    ws = wb["KPI"]
    vals = [r[1].value for r in ws.iter_rows() if r[0].value == "throughput_per_hour"]
    assert vals[0] == baseline["sim"].kpis.throughput_per_hour   # KPI tablosu
    assert baseline["sim"].kpis.throughput_per_hour in [r[2].value for r in ws.iter_rows()
                                                        if r[0].value == "throughput_per_hour"]   # iyileştirme tablosu
    ft = wb["From-To"]
    assert any(c.value == baseline["flow"].cells[0].from_id for row in ft.iter_rows() for c in row)
    cmp_ws = wb["Karşılaştırma"]
    assert any(c.value == "flow.total_distance_m_per_hour" for row in cmp_ws.iter_rows() for c in row)


def test_png_export(exported):
    _, files = exported
    names = {p.name for p in files["png"]}
    assert any(n.startswith("layout_current") for n in names)
    assert any(n.startswith("spaghetti_current") for n in names)
    assert any(n.startswith("before_after") for n in names)
    for p in files["png"]:
        assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert zipfile.is_zipfile(exported[1]["xlsx"])
    assert isinstance(exported[1]["xlsx"], Path)
