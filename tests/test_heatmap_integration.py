"""Isı haritası entegrasyonu: rapor figürleri, Excel sayfaları, Streamlit sayfaları (gerçek demo)."""
from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook
from streamlit.testing.v1 import AppTest

from backend.analytics.heatmap_figures import time_grid_figure
from backend.analytics.heatmaps import station_time_utilization
from backend.database.session import configure_database
from backend.demo.demo_factory import build_demo_project
from backend.layout import editing
from backend.layout.validation import validate_layout
from backend.reporting import builder
from backend.routing.engine import compute_flow_metrics, compute_routes, derive_flow_edges
from backend.schemas import LayoutAlternative, OptimizationConfig, OptimizationResult
from backend.simulation.engine import run_simulation
from frontend import state as fstate

APP = str(Path(__file__).resolve().parent.parent / "frontend" / "streamlit_app.py")


def _eval(project, layout):
    edges = project.flow_edges or derive_flow_edges(project)
    routes = compute_routes(layout, edges)
    flow = compute_flow_metrics(layout, edges, routes, project)
    sim = run_simulation(project.model_copy(update={"layout": layout}, deep=True), routes)
    return flow, sim


@pytest.fixture(scope="module")
def project():
    return build_demo_project()


@pytest.fixture(scope="module")
def base(project):
    return _eval(project, project.layout)


@pytest.fixture(scope="module")
def opt(project, base):
    lay = editing.clone_layout(editing.move_workstation(project.layout, "asm", 17, 14), "alt-1", "Alt 1")
    rep = validate_layout(lay)
    assert rep.valid
    flow, sim = _eval(project, lay)
    d0 = base[0].total_distance_m_per_hour
    alt = LayoutAlternative(id="alt-1", name="Alt 1", algorithm="manual_test", layout=lay, validation=rep,
                            objective=flow.total_distance_m_per_hour, flow=flow, sim=sim,
                            distance_reduction_pct=(d0 - flow.total_distance_m_per_hour) / d0 * 100,
                            moved_station_ids=["asm"])
    return OptimizationResult(baseline_layout_id=project.layout.id, baseline_flow=base[0], baseline_sim=base[1],
                              alternatives=[alt], best_alternative_id="alt-1", rationale="test",
                              config=OptimizationConfig(algorithm="manual_test", seed=7))


@pytest.fixture(scope="module")
def ctx(project, base, opt):
    return builder.build_report_context(project, base[1], opt)


def _fig_sections(ctx):
    return {s["id"]: s["figures"] for s in ctx["sections"]}


def test_context_has_heatmap_figures(ctx):
    f = _fig_sections(ctx)
    assert "heatmap_from_to" in f["material_flow"]
    assert "heatmap_util" in f["sim_results"] and "heatmap_queue" in f["bottleneck"]
    assert "heatmap_compare" in f["scenario_comparison"]
    for k in ("heatmap_from_to", "heatmap_util", "heatmap_queue", "heatmap_compare"):
        assert k in ctx["_figures"]


def test_methodology_has_technical_queue_definition(ctx):
    text = " ".join(p for s in ctx["sections"] if s["id"] == "methodology" for p in s["paragraphs"])
    assert "operatör" in text and "QUEUE_ENTERED" in text and "maks. kuyruk" in text


def test_user_visible_figure_text_has_no_internal_names(ctx):
    for k in ("heatmap_util", "heatmap_queue", "heatmap_from_to"):
        fig = ctx["_figures"][k]["fig"]
        txt = " ".join(str(a.text) for a in fig.layout.annotations)
        for bad in ("PROCESS_STARTED", "QUEUE_ENTERED", "FlowMetrics", "StationStats", "queue_len"):
            assert bad not in txt, (k, bad)


def _find(ws, text, col=1):
    return next(r for r in range(1, 12) if ws.cell(row=r, column=col).value == text)


def test_excel_sheets_match_objects(ctx, base, tmp_path):
    sim = base[1]
    wb = load_workbook(builder.export_excel(ctx, tmp_path))
    for name in ("Saatlik Doluluk", "Saatlik Kuyruk", "From-To Matrisi"):
        assert name in wb.sheetnames
    grid = ctx["_objects"]["util_grid"]
    ws = wb["Saatlik Doluluk"]
    hdr = _find(ws, "İstasyon kimliği")
    assert "oran" in ws.cell(row=hdr, column=3).value
    for i, vals in enumerate(grid.values, start=hdr + 1):
        assert [ws.cell(row=i, column=3 + j).value for j in range(len(vals))] == pytest.approx(vals, rel=1e-12)
    # tam ufukta ağırlıklı ortalama = StationStats.utilization (tek replikasyon, ısınma yok)
    assert sim.config.replications == 1 and sim.config.warmup_s == 0
    widths = [b - a for a, b in zip(grid.bin_edges_s[:-1], grid.bin_edges_s[1:], strict=True)]
    by = {s.station_id: s.utilization for s in sim.stations}
    for i, sid in enumerate(grid.station_ids, start=hdr + 1):
        row = [ws.cell(row=i, column=3 + j).value for j in range(len(widths))]
        avg = sum(v * w for v, w in zip(row, widths, strict=True)) / sum(widths)
        assert avg == pytest.approx(by[sid], abs=1e-6), sid
    q = wb["Saatlik Kuyruk"]
    qa = ctx["_objects"]["queue_avg_grid"]
    qh = _find(q, "İstasyon kimliği")
    assert [q.cell(row=qh + 1, column=3 + j).value for j in range(len(qa.values[0]))] == pytest.approx(qa.values[0], rel=1e-12)
    assert "adet" in q.cell(row=qh, column=3).value


def test_from_to_sheet_total_1540(ctx, tmp_path):
    wb = load_workbook(builder.export_excel(ctx, tmp_path))
    ws = wb["From-To Matrisi"]
    ft = ctx["_objects"]["from_to"]
    n = len(ft.station_names)
    hdr = _find(ws, "Nereden / Nereye")
    assert "hareket/sa" in ws.cell(row=hdr, column=2).value
    r2 = hdr + n + 3  # ikinci blok = m/sa
    assert ws.cell(row=r2, column=1).value == "Nereden / Nereye"
    assert "m/sa" in ws.cell(row=r2, column=2).value
    total = sum(ws.cell(row=r2 + 1 + i, column=2 + j).value or 0 for i in range(n) for j in range(n))
    assert total == pytest.approx(1540.0, abs=1e-6)
    assert total == pytest.approx(ft.total_metres_per_hour)


def test_no_opt_no_compare_figure_and_no_crash(project, base, tmp_path):
    c = builder.build_report_context(project, base[1], None)
    assert "heatmap_compare" not in c["_figures"]
    assert not any("heatmap_compare" in s["figures"] for s in c["sections"])
    assert builder.export_html(c, tmp_path).stat().st_size > 0


def test_exports_include_heatmap_images(ctx, tmp_path):
    html = builder.export_html(ctx, tmp_path / "h").read_text(encoding="utf-8")
    assert "Saatlik doluluk" in html and "Saatlik ortalama kuyruk" in html and "From-To ısı haritası" in html
    assert html.count("data:image/png;base64,") >= 9
    pngs = {p.name for p in builder.export_png(ctx, tmp_path / "p")}
    assert any(n.startswith("heatmap_util") for n in pngs) and any(n.startswith("heatmap_compare") for n in pngs)
    assert builder.export_pdf(ctx, tmp_path / "d").read_bytes()[:4] == b"%PDF"


def test_footer_fits_inside_bottom_margin(base, project):
    """Dipnot, alt kenar boşluğu içine satır sayısı kadar yer ayrılarak sığar (1200x650'de kırpılmaz)."""
    fig = time_grid_figure(station_time_utilization(base[1], project.layout, 3600.0))
    ann = [a for a in fig.layout.annotations if a.yref == "paper" and a.y == 0 and a.yshift]
    assert ann
    lines = ann[0].text.count("<br>") + 1
    assert -ann[0].yshift + 15 * lines <= fig.layout.margin.b


# --------------------------------------------------------------------------- Streamlit
@pytest.fixture()
def app(tmp_path, monkeypatch):
    configure_database("sqlite:///:memory:")
    monkeypatch.setattr(fstate, "REPORT_DIR", tmp_path / "reports")
    at = AppTest.from_file(APP, default_timeout=180)
    at.run()
    return at


def _btn(at, key):
    return next(b for b in at.button if b.key == key)


def _charts(at):
    return len(at.get("plotly_chart"))


def test_streamlit_pages_with_heatmaps(app):
    _btn(app, "top_demo").click().run()
    before = {}
    for page in ("Simulation", "Process Flow", "Scenario Comparison"):
        app.sidebar.radio[0].set_value(page).run()
        assert not app.exception, page
        before[page] = _charts(app)
    _btn(app, "top_sim").click().run()
    assert not app.exception and not app.error
    app.sidebar.radio[0].set_value("Simulation").run()
    assert not app.exception and not app.error
    assert _charts(app) >= before["Simulation"] + 2
    app.sidebar.radio[0].set_value("Process Flow").run()
    assert not app.exception and not app.error and _charts(app) >= 1
    app.radio(key="pf_value").set_value("metres").run()
    assert not app.exception and not app.error
    # ikinci koşu (farklı seed) -> Scenario Comparison'da karşılaştırma ısı haritası
    app.sidebar.radio[0].set_value("Simulation").run()
    app.number_input(key="sim_seed").set_value(43)
    next(b for b in app.button if b.label == "Simülasyonu çalıştır").click().run()
    assert not app.exception and not app.error
    app.sidebar.radio[0].set_value("Scenario Comparison").run()
    assert not app.exception and not app.error
    assert _charts(app) > before["Scenario Comparison"]
