"""[QA] Kontrol 4 — rapor <-> hesap tutarlılığı.

HTML/Excel/PDF çıktılarının METNİNDEN/HÜCRELERİNDEN sayılar çıkarılıp SimResult / FlowMetrics / ScenarioComparison ile
eşleştirilir (rapor bağlamındaki `numbers` sözlüğüne GÜVENİLMEZ). PDF metni `pdftotext` varsa okunur.
"""
from __future__ import annotations

import base64
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

from backend.reporting import builder

NOT_COMPUTED = "hesaplanamadı"
SECTION_TITLES = builder.SECTION_TITLES


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables: dict[str, list[list[str]]] = {}
        self.texts: list[str] = []
        self.imgs: list[str] = []
        self.h2: list[str] = []
        self._cap = ""
        self._rows: list[list[str]] = []
        self._cell: list[str] | None = None
        self._row: list[str] | None = None
        self._in_cap = False
        self._in_h2 = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self._rows, self._cap = [], ""
        elif tag == "caption":
            self._in_cap = True
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
        elif tag == "img":
            self.imgs.append(a.get("src", ""))
        elif tag == "h2":
            self._in_h2 = True
            self.h2.append("")

    def handle_endtag(self, tag):
        if tag == "caption":
            self._in_cap = False
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._rows.append(self._row)
            self._row = None
        elif tag == "table":
            self.tables[self._cap] = self._rows
        elif tag == "h2":
            self._in_h2 = False

    def handle_data(self, data):
        if self._in_cap:
            self._cap += data
        if self._in_h2:
            self.h2[-1] += data
        if self._cell is not None:
            self._cell.append(data)
        self.texts.append(data)


def parse_html(path) -> _Tables:
    p = _Tables()
    p.feed(path.read_text(encoding="utf-8"))
    return p


def num(s: str) -> float:
    return float(s.replace(",", ""))


@pytest.fixture(scope="module")
def html(report_bundle):
    return parse_html(report_bundle["html"])


# ----------------------------------------------------------------------------------------- HTML
def test_html_has_14_sections_in_order_and_images(html):
    assert [h.split(". ", 1)[1] for h in html.h2] == SECTION_TITLES
    assert len(html.imgs) >= 8
    for src in html.imgs:
        assert src.startswith("data:image/png;base64,")
        assert base64.b64decode(src.split(",", 1)[1])[:8] == b"\x89PNG\r\n\x1a\n"
    assert any("ÖRNEK / VARSAYIMSAL" in t for t in html.texts)


def test_html_kpi_table_equals_simresult(html, analysis):
    k = analysis.baseline_sim.kpis.model_dump()
    rows = {r[0]: r[1] for r in html.tables["KPI'lar (baseline)"][1:]}
    assert set(rows) == set(k)
    for name, v in k.items():
        assert num(rows[name]) == pytest.approx(v, abs=0.006), name      # tablo 2 ondalık gösterir
    assert num(rows["completed"]) == analysis.baseline_sim.kpis.completed == 62
    assert num(rows["throughput_per_hour"]) == pytest.approx(7.75)


def test_html_executive_summary_numbers(html, analysis):
    text = " ".join(html.texts)
    flow, sim = analysis.flow, analysis.baseline_sim
    assert f"{flow.total_distance_m_per_hour:.1f} m/saat" in text
    assert f"ürün başına {flow.distance_per_unit_m:.2f} m" in text
    assert f"seed {sim.seed}" in text
    assert f"verim {sim.kpis.throughput_per_hour:.2f} adet/saat" in text
    assert f"ortalama WIP {sim.kpis.avg_wip:.2f}" in text
    assert f"Darboğaz adayı: {analysis.bottlenecks['bottleneck_station_id']}" in text
    best = next(a for a in analysis.optimization.alternatives if a.id == analysis.optimization.best_alternative_id)
    assert f"{best.flow.total_distance_m_per_hour:.1f} m/saat" in text


def test_html_flow_tables_equal_flowmetrics(html, analysis):
    flow = analysis.flow
    rows = html.tables["From-To tablosu"][1:]
    assert len(rows) == len(flow.cells)
    for r, c in zip(rows, flow.cells, strict=True):
        assert (r[0], r[1]) == (c.from_id, c.to_id)
        assert num(r[2]) == pytest.approx(c.moves_per_hour, abs=0.006)
        assert num(r[3]) == pytest.approx(c.distance_m, abs=0.006)
        assert num(r[4]) == pytest.approx(c.metres_per_hour, abs=0.006)
    veh = {r[0]: num(r[1]) for r in html.tables["Araç bazında mesafe (m/saat)"][1:]}
    assert veh["Toplam (D_total)"] == pytest.approx(1540.0)
    assert veh["Operatör yürüyüşü"] == pytest.approx(flow.operator_walking_m_per_hour)
    assert veh["Forklift"] == pytest.approx(flow.forklift_m_per_hour)


def test_html_station_stats_and_bottleneck_ranking(html, analysis):
    sim = analysis.baseline_sim
    rows = {r[0]: r for r in html.tables["İstasyon istatistikleri"][1:]}
    for s in sim.stations:
        r = rows[s.station_id]
        assert num(r[1]) == pytest.approx(s.utilization, abs=0.006)
        assert int(num(r[4])) == s.processed
        assert int(num(r[3])) == s.max_queue_len
    rank = html.tables["Darboğaz sıralaması"][1:]
    assert rank[0][1] == analysis.bottlenecks["bottleneck_station_id"] == sim.bottleneck_station_id


def test_html_comparison_and_alternatives_match(html, analysis):
    cmp = analysis.comparison
    rows = {r[0]: r for r in html.tables["KPI karşılaştırması"][1:]}
    assert set(rows) == {d.name for d in cmp.deltas}
    for d in cmp.deltas:
        r = rows[d.name]
        assert num(r[2]) == pytest.approx(d.baseline, abs=0.006)
        assert num(r[3]) == pytest.approx(d.scenario, abs=0.006)
        assert num(r[4]) == pytest.approx(d.delta_abs, abs=0.006)
    alts = {r[0]: r for r in html.tables["Alternatifler"][1:]}
    for a in analysis.optimization.alternatives:
        assert num(alts[a.id][4]) == pytest.approx(a.flow.total_distance_m_per_hour, abs=0.006)
    text = " ".join(html.texts)
    assert "Aynı seed (CRN): evet" in text and "seed 42" in text


def test_html_methodology_records_seeds(html, analysis):
    meth = {r[0]: r[1] for r in html.tables["Parametreler"][1:]}
    assert meth["Baseline seed"] == "42"
    assert meth["Optimizasyon seed"] == str(analysis.optimization.config.seed)
    assert meth["Replikasyon"] == "1"
    sc = {r[0]: r[1] for r in html.tables["Simülasyon konfigürasyonu"][1:]}
    assert sc["Seed"] == "42" and num(sc["Süre (s)"]) == 28800


def test_html_savings_present_with_assumed_costs(html, analysis):
    sv = analysis.comparison.savings
    rows = {r[0]: num(r[1]) for r in html.tables["Taşıma maliyeti tasarrufu (TRY/saat)"][1:]}
    assert rows["tasarruf"] == pytest.approx(sv["travel_cost_per_hour"], abs=0.006)
    text = " ".join(html.texts)
    assert "VARSAYIMSALDIR" in text and f"ROI {NOT_COMPUTED}" in text   # ROI hiç uydurulmaz


# ----------------------------------------------------------------------------------------- Excel
def test_excel_cells_equal_simresult_and_flow(report_bundle, analysis):
    from openpyxl import load_workbook

    wb = load_workbook(report_bundle["xlsx"], data_only=True)
    assert wb.sheetnames == ["Proje", "KPI", "İstasyonlar", "From-To", "Darboğaz", "Olay Özeti", "Alternatifler",
                             "Karşılaştırma", "Metodoloji", "Saatlik Doluluk", "Saatlik Kuyruk", "From-To Matrisi"]
    kpi_rows: dict = {}
    for r in wb["KPI"].iter_rows(values_only=True):       # ilk tablo = baseline KPI'ları (sonrakiler 'improvement')
        if r and r[0]:
            kpi_rows.setdefault(r[0], r[1])
    k = analysis.baseline_sim.kpis
    assert kpi_rows["throughput_per_hour"] == pytest.approx(k.throughput_per_hour)
    assert kpi_rows["completed"] == k.completed
    assert kpi_rows["avg_wip"] == pytest.approx(k.avg_wip)
    assert kpi_rows["avg_cycle_time_s"] == pytest.approx(k.avg_cycle_time_s)
    ft = {r[0]: r for r in wb["From-To"].iter_rows(values_only=True) if r and r[0]}
    assert ft["Toplam (D_total)"][1] == pytest.approx(analysis.flow.total_distance_m_per_hour)
    meth = {r[0]: r[1] for r in wb["Metodoloji"].iter_rows(values_only=True) if r and r[0]}
    assert meth["Baseline seed"] == 42
    bn = [r for r in wb["Darboğaz"].iter_rows(values_only=True) if r and r[0] == 1]
    assert bn and bn[0][1] == analysis.bottlenecks["bottleneck_station_id"]
    comp = {r[0]: r for r in wb["Karşılaştırma"].iter_rows(values_only=True) if r and r[0]}
    assert comp["throughput_per_hour"][3] == pytest.approx(
        next(d.scenario for d in analysis.comparison.deltas if d.name == "throughput_per_hour"))


# ----------------------------------------------------------------------------------------- PDF
@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext yok")
def test_pdf_text_contains_computed_numbers(report_bundle, analysis):
    txt = subprocess.run(["pdftotext", "-layout", str(report_bundle["pdf"]), "-"], capture_output=True, text=True,
                         check=True).stdout
    flow, sim = analysis.flow, analysis.baseline_sim
    assert f"{flow.total_distance_m_per_hour:.1f} m/saat" in txt
    assert f"verim {sim.kpis.throughput_per_hour:.2f} adet/saat" in txt.replace("\n", " ")
    assert "seed 42" in txt and "Darboğaz adayı: drill" in txt
    for title in SECTION_TITLES:
        assert title in txt, title
    assert "ÖRNEK / VARSAYIMSAL VERİ" in txt


@pytest.mark.skipif(shutil.which("pdfinfo") is None, reason="pdfinfo yok")
def test_pdf_pages_and_images(report_bundle):
    info = subprocess.run(["pdfinfo", str(report_bundle["pdf"])], capture_output=True, text=True, check=True).stdout
    pages = int(re.search(r"Pages:\s+(\d+)", info).group(1))
    assert pages >= 5
    if shutil.which("pdfimages"):
        imgs = subprocess.run(["pdfimages", "-list", str(report_bundle["pdf"])], capture_output=True, text=True).stdout
        assert len(imgs.strip().splitlines()) - 2 >= 8      # başlık 2 satır; >= 8 grafik görseli


# ----------------------------------------------------------------------------------------- maliyet yok
@pytest.fixture(scope="module")
def nocost_bundle(demo_project, analysis, tmp_path_factory):
    proj = demo_project.model_copy(update={"cost": None}, deep=True)
    out = tmp_path_factory.mktemp("nocost")
    ctx = builder.build_report_context(proj, analysis.baseline_sim, analysis.optimization)
    return {"ctx": ctx, "html": builder.export_html(ctx, out), "xlsx": builder.export_excel(ctx, out),
            "pdf": builder.export_pdf(ctx, out), "project": proj}


def test_cost_none_savings_and_roi_not_computed(nocost_bundle, analysis):
    ctx = nocost_bundle["ctx"]
    assert "travel_cost_saving_per_hour" not in ctx["numbers"]
    h = parse_html(nocost_bundle["html"])
    text = " ".join(h.texts)
    assert f"Tasarruf ve ROI {NOT_COMPUTED}" in text
    assert not any("tasarrufu" in cap for cap in h.tables)             # hiç tasarruf tablosu yok
    assert f"Taşıma maliyeti {NOT_COMPUTED}" in text
    # tüm cost-dolu sayılar kaynakta da yok
    from backend.analytics.bottleneck import compare_results

    best = next(a for a in analysis.optimization.alternatives if a.id == analysis.optimization.best_alternative_id)
    cmp = compare_results(analysis.baseline_sim, best.sim, analysis.flow, best.flow, nocost_bundle["project"])
    assert cmp.savings is None
    from openpyxl import load_workbook

    wb = load_workbook(nocost_bundle["xlsx"], data_only=True)
    kpi_text = " ".join(str(c) for r in wb["KPI"].iter_rows(values_only=True) for c in r if c is not None)
    assert "tasarruf" not in kpi_text.lower().replace("tasarruf ve roi", "") or NOT_COMPUTED in kpi_text


def test_cost_none_pipeline_comparison_and_agent_tool(demo_project, analysis, db, tmp_path):
    from backend import services
    from backend.agent.tool_executor import ToolExecutor
    from backend.agent.tool_registry import ToolContext
    from backend.schemas import ToolStatus

    proj = services.get_project("demo-project")
    services.save_project(proj.model_copy(update={"cost": None}, deep=True))
    assert services.get_project("demo-project").cost is None
    ex = ToolExecutor(ToolContext(project_id="demo-project", artifact_dir=tmp_path))
    tid = ex.start_task("t", "demo-project")
    opt_id = services.save_optimization("demo-project", analysis.optimization)
    r = ex.execute(tid, "calculate_savings", {"optimization_id": opt_id})
    assert r.status == ToolStatus.ERROR and r.error_code == "NO_COST_CONFIG" and "hesaplanamaz" in r.error
    from backend.analytics.bottleneck import compare_results

    best = next(a for a in analysis.optimization.alternatives if a.id == analysis.optimization.best_alternative_id)
    flow_nocost = best.flow.model_copy(update={"material_handling_cost_per_hour": None})
    cmp = compare_results(analysis.baseline_sim, best.sim, analysis.flow, flow_nocost,
                          proj.model_copy(update={"cost": None}))
    assert cmp.savings is None


def test_report_without_optimization_says_not_run(demo_project, analysis, tmp_path):
    ctx = builder.build_report_context(demo_project, analysis.baseline_sim, None)
    text = " ".join(" ".join(s["paragraphs"] + s["bullets"]) for s in ctx["sections"])
    assert "Optimizasyon çalıştırılmadı" in text and "Karşılaştırma yapılamadı" in text
    assert f"Tasarruf ve ROI {NOT_COMPUTED}" in text
    assert "best_alternative_id" not in ctx["numbers"]
    assert len(ctx["sections"]) == 14


def test_ai_text_is_included_verbatim_and_labelled(demo_project, analysis):
    ctx = builder.build_report_context(demo_project, analysis.baseline_sim, analysis.optimization,
                                       "Gemini yorumu: darboğaz drill.")
    sec = next(s for s in ctx["sections"] if s["id"] == "ai_recommendations")
    assert "Gemini yorumu: darboğaz drill." in sec["paragraphs"]
    assert any("hesaplanmış metriklere dayanmalıdır" in p for p in sec["paragraphs"])
