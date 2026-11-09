# ruff: noqa: F811
"""[QA] Uçtan uca doğrulama (kontrol listesi 1, 10): pipeline, FakeGemini çok adımlı tool zinciri, Gemini'siz yol,
master prompt §16 (10 adım) ve §21 başarı tanımı kanıtları.

Bağımsızdır: geliştirici testlerinin yardımcılarını kullanmaz. Ağ/gerçek Gemini yok.
Bilinen/yeniden üretilmiş kusurlar `xfail(strict=True)` ile işaretlidir (QA_REPORT.md'deki F-numaraları).
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from backend import services
from backend.agent.orchestrator import FactoryFlowAgent
from backend.agent.tool_executor import ToolExecutor
from backend.agent.tool_registry import TOOL_NAMES, ToolContext
from backend.config import Settings, reset_settings_cache
from backend.database import configure_database
from backend.schemas import GeminiStatus, ToolStatus
from tests.fakes import FakeGeminiClient, Step
from tests.qa.conftest import _no_gemini_key, analysis, db, demo_project, report_bundle  # noqa: F401  (fixture'lar)

USER_REQUEST = ("Fabrikamı analiz et, spaghetti chart oluştur, SimPy simülasyonu çalıştır, daha verimli bir layout "
                "öner ve mevcut durumla karşılaştırmalı mühendislik raporu hazırla.")


# ----------------------------------------------------------------------------------------- pipeline
def test_pipeline_all_steps_run_for_demo(analysis):
    """Kontrol 1a: analyze_facility(demo, optimize=True) — tüm adımlar ok, gerçek nesneler dolu."""
    names = [s.step for s in analysis.steps]
    assert names == ["validate_layout", "material_flow_routes", "spaghetti_chart", "simulation",
                     "bottleneck_analysis", "optimization", "scenario_comparison"]
    assert all(s.ok for s in analysis.steps)
    assert analysis.validation.valid
    assert analysis.flow is not None and analysis.baseline_sim is not None
    assert analysis.optimization is not None and analysis.comparison is not None
    assert len(analysis.artifacts) == 1 and Path(analysis.artifacts[0]).stat().st_size > 10_000
    assert analysis.bottlenecks["bottleneck_station_id"] == analysis.baseline_sim.bottleneck_station_id


def test_pipeline_deterministic_across_runs(demo_project, analysis):
    """Aynı girdi + aynı seed -> aynı sayılar (optimizasyonsuz hızlı yol)."""
    from backend.pipeline import analyze_facility

    again = analyze_facility(demo_project, optimize=False)
    assert again.flow.total_distance_m_per_hour == analysis.flow.total_distance_m_per_hour
    assert again.baseline_sim.kpis == analysis.baseline_sim.kpis


# ----------------------------------------------------------------------------------------- agent zinciri (FakeGemini)
@pytest.fixture(scope="module")
def chain(tmp_path_factory):
    """Çok adımlı FakeGemini zinciri BİR kez çalışır; sonuçlar saklanır."""
    tmp = tmp_path_factory.mktemp("chain")
    mp = pytest.MonkeyPatch()
    mp.delenv("GEMINI_API_KEY", raising=False)
    reset_settings_cache()
    configure_database(f"sqlite:///{tmp / 'chain.db'}")
    services.ensure_demo_project()
    layout_before = services.get_layout("demo-project").model_dump()
    script = [
        Step.call("validate_layout", {}, ("calculate_material_flow", {}), ("create_spaghetti_chart", {})),
        Step.call("run_simpy_simulation", {}, ("analyze_bottlenecks", {})),
        Step.call("optimize_factory_layout", {"apply_best": True}),
        Step.call("compare_scenarios", {}, ("calculate_savings", {})),
        Step.call("generate_engineering_report", {"format": "html"}, ("generate_engineering_report", {"format": "pdf"}),
                  ("export_excel_report", {})),
        Step.text("Analiz tamamlandı; sayılar araç çıktılarından alınmıştır."),
    ]
    client = FakeGeminiClient(script)
    settings = Settings(external_data_consent=True)
    ctx = ToolContext(artifact_dir=tmp / "artifacts")
    agent = FactoryFlowAgent(client, ToolExecutor(ctx), settings=settings)
    reply = agent.handle(USER_REQUEST, "demo-project")
    out = {
        "reply": reply, "client": client, "agent": agent, "tmp": tmp,
        "layout_before": layout_before,
        "layout_after": services.get_layout("demo-project").model_dump(),
        "versions_after": services.list_layout_versions("demo-project"),
        "task": services.get_agent_task(reply.task_id),
        "latest_sim": services.get_latest_sim("demo-project"),
        "sim_runs": services.list_simulation_runs("demo-project"),
    }
    yield out
    mp.undo()


def _tool_results(chain):
    """Fake Gemini'nin TURN olarak gördüğü tool sonuçları: {tool_name: [response,...]}"""
    res: dict[str, list[dict]] = {}
    for turn in chain["client"].calls[-1]["turns"]:
        for fr in turn.function_results:
            res.setdefault(fr.name, []).append(fr.response)
    return res


def test_chain_executes_every_tool_for_real(chain):
    rep = chain["reply"]
    assert [r.tool for r in rep.tool_calls] == [
        "validate_layout", "calculate_material_flow", "create_spaghetti_chart", "run_simpy_simulation",
        "analyze_bottlenecks", "optimize_factory_layout", "compare_scenarios", "calculate_savings",
        "generate_engineering_report", "generate_engineering_report", "export_excel_report"]
    statuses = {r.tool: r.status for r in rep.tool_calls}
    assert statuses["optimize_factory_layout"] == ToolStatus.NEEDS_APPROVAL
    assert all(r.status in (ToolStatus.OK, ToolStatus.NEEDS_APPROVAL) for r in rep.tool_calls), \
        [(r.tool, r.status, r.error) for r in rep.tool_calls]
    assert all(r.duration_ms > 0 for r in rep.tool_calls)
    assert rep.requests_used == 6 and rep.text.startswith("Analiz tamamlandı")


def test_chain_tool_numbers_match_independent_computation(chain, analysis):
    """Tool çıktısındaki sayılar bağımsız (pipeline) hesapla aynı -> tool gerçek domain'i çağırıyor."""
    res = _tool_results(chain)
    flow = res["calculate_material_flow"][0]
    blob = json.dumps(flow, ensure_ascii=False)
    assert str(analysis.flow.total_distance_m_per_hour) in blob or "1540" in blob
    sim = res["run_simpy_simulation"][0]["data"]
    assert sim["kpis"]["throughput_per_hour"] == pytest.approx(analysis.baseline_sim.kpis.throughput_per_hour)
    assert sim["kpis"]["completed"] == analysis.baseline_sim.kpis.completed
    assert sim["seed"] == analysis.baseline_sim.seed
    bn = res["analyze_bottlenecks"][0]["data"]["analysis"]
    assert bn["bottleneck_station_id"] == analysis.bottlenecks["bottleneck_station_id"]
    opt = res["optimize_factory_layout"][0]["data"]
    assert opt["best_alternative_id"] == analysis.optimization.best_alternative_id
    assert len(opt["alternatives"]) >= 3 and opt["applied"] is False


def test_chain_artifacts_exist_and_are_real_files(chain):
    arts = chain["reply"].artifacts
    exts = sorted(Path(a).suffix for a in arts)
    assert exts == [".html", ".html", ".pdf", ".xlsx"], arts   # spaghetti + rapor html + pdf + xlsx
    for a in arts:
        p = Path(a)
        assert p.exists() and p.stat().st_size > 5_000, a
    pdf = next(Path(a) for a in arts if a.endswith(".pdf"))
    assert pdf.read_bytes()[:5] == b"%PDF-"
    xlsx = next(Path(a) for a in arts if a.endswith(".xlsx"))
    assert zipfile.is_zipfile(xlsx)
    report_html = next(Path(a) for a in arts if a.endswith(".html") and "report_" in a)
    assert "Mühendislik Raporu" in report_html.read_text(encoding="utf-8")


def test_chain_does_not_change_saved_layout_without_approval(chain):
    assert chain["reply"].pending_changes and len(chain["reply"].pending_changes) == 1
    assert chain["layout_after"] == chain["layout_before"]
    assert len(chain["versions_after"]) == 1
    assert "UYGULANMADI" in chain["reply"].text


def test_chain_tasks_and_calls_logged_in_db(chain):
    t = chain["task"]
    assert t["id"] == chain["reply"].task_id
    assert len(t["tool_calls"]) == 11
    assert all("duration_ms" in c and "status" in c for c in t["tool_calls"])
    assert len(chain["sim_runs"]) == 1     # sim kaydı DB'de


def test_chain_gemini_got_tool_results_before_final_text(chain):
    """Son generate çağrısında model, tüm tool sonuçlarını görmüş olmalı (açıklama hesaplamaya dayanır)."""
    last_turns = chain["client"].calls[-1]["turns"]
    assert sum(len(t.function_results) for t in last_turns) == 11
    assert "approve_change" not in chain["client"].calls[0]["tool_names"]
    assert set(chain["client"].calls[0]["tool_names"]) == set(TOOL_NAMES)


# ----------------------------------------------------------------------------------------- Gemini'siz (offline)
def test_offline_agent_runs_full_analysis_without_key_or_client(db, tmp_path):
    ctx = ToolContext(artifact_dir=tmp_path / "art")
    agent = FactoryFlowAgent(None, ToolExecutor(ctx))
    reply = agent.handle(USER_REQUEST, "demo-project")
    assert reply.gemini_status == GeminiStatus.DISABLED
    assert reply.requests_used == 0 and reply.tokens_used == 0
    t = reply.text
    assert "AI asistanı devre dışı" in t and "ÖRNEK / VARSAYIMSAL" in t
    assert "1540.0 m/saat" in t and "seed=42" in t and "Darboğaz istasyonu: drill" in t
    assert "alternatif üretildi" in t
    assert any(Path(a).exists() for a in reply.artifacts)


def test_offline_with_real_client_without_key_never_calls_sdk(db, tmp_path):
    from backend.agent.gemini_client import GeminiClient

    s = Settings(gemini_api_key=None, external_data_consent=True)
    assert s.gemini_configured is False
    client = GeminiClient(s)
    assert client.status == GeminiStatus.API_KEY_MISSING
    agent = FactoryFlowAgent(client, ToolExecutor(ToolContext(artifact_dir=tmp_path)), settings=s)
    reply = agent.handle("Bu tesisi analiz et", "demo-project")
    assert reply.gemini_status == GeminiStatus.API_KEY_MISSING and "1540.0" in reply.text
    assert client._sdk is None   # SDK hiç yaratılmadı


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F-01: offline 'tesisi analiz et' sonucu DB'ye yazmıyor ve rapor üretmiyor (§16 adım 10)")
def test_offline_analysis_produces_report_like_step_10(db, tmp_path):
    reply = FactoryFlowAgent(None, ToolExecutor(ToolContext(artifact_dir=tmp_path / "a"))).handle(
        "Bu tesisi analiz et", "demo-project")
    kinds = {Path(a).suffix for a in reply.artifacts}
    assert ".pdf" in kinds or any("report_" in a for a in reply.artifacts)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F-01: offline analiz simülasyon/optimizasyon kaydı bırakmıyor; Raporlar sayfası 'önce simülasyon çalıştırın' diyor")
def test_offline_analysis_persists_results_for_reports_page(db, tmp_path):
    FactoryFlowAgent(None, ToolExecutor(ToolContext(artifact_dir=tmp_path / "a"))).handle(
        "Bu tesisi analiz et", "demo-project")
    assert services.get_latest_sim("demo-project") is not None
    assert services.get_latest_optimization("demo-project") is not None


# ----------------------------------------------------------------------------------------- §16 demo senaryosu
def test_demo_is_labelled_and_matches_spec(demo_project):
    p = demo_project
    assert p.layout.facility.width == 30 and p.layout.facility.height == 20
    assert [w.name for w in p.layout.workstations] and len(p.layout.workstations) == 7
    names = {w.name for w in p.layout.workstations}
    assert names == {"Raw Material", "Cutting", "Drilling", "Assembly", "Quality Control", "Packaging", "Shipping"}
    res = {r.vehicle.value: r.count for r in p.sim_config.resources}
    assert res == {"operator": 2, "forklift": 1}
    assert "ÖRNEK" in p.data_label and "VARSAYIMSAL" in p.data_label and p.is_demo


def test_s16_ten_steps_each_have_evidence(analysis, report_bundle):
    a = analysis
    # 1 doğrula
    assert a.validation.valid
    # 2 malzeme akışı
    assert a.flow.total_distance_m_per_hour == pytest.approx(1540.0)
    # 3 spaghetti
    assert Path(a.artifacts[0]).read_text(encoding="utf-8").lower().count("plotly") > 0
    # 4 simpy
    assert a.baseline_sim.kpis.completed > 0 and a.baseline_sim.events
    # 5 darboğaz
    assert a.bottlenecks["bottleneck_station_id"] == "drill"
    # 6 en az bir optimize alternatif
    assert len(a.optimization.alternatives) >= 1
    # 7 karşılaştırılabilir koşullarda yeni simülasyon (CRN)
    best = next(x for x in a.optimization.alternatives if x.id == a.optimization.best_alternative_id)
    assert best.sim is not None and a.comparison.same_seed and best.sim.seed == a.baseline_sim.seed
    # 8 KPI karşılaştırması
    assert any(d.name == "throughput_per_hour" for d in a.comparison.deltas)
    # 9 mühendislik özeti
    from backend.agent.offline import summarize_analysis
    from backend.demo.demo_factory import build_demo_project

    assert "toplam taşıma mesafesi 1540.0" in summarize_analysis(a, build_demo_project())
    # 10 HTML/PDF raporu
    assert report_bundle["html"].stat().st_size > 50_000
    assert report_bundle["pdf"].read_bytes()[:5] == b"%PDF-"


def test_s21_success_definition_checklist(chain):
    """§21: veriyi kontrol et, gerçek araçları çağır, simülasyon, spaghetti, alternatif layout, karşılaştırma, KPI,
    Gemini açıklaması, indirilebilir rapor."""
    tools = {r.tool for r in chain["reply"].tool_calls}
    need = {"validate_layout", "calculate_material_flow", "run_simpy_simulation", "create_spaghetti_chart",
            "optimize_factory_layout", "compare_scenarios", "generate_engineering_report", "export_excel_report"}
    assert need <= tools
    assert chain["reply"].text and chain["reply"].artifacts
