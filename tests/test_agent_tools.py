"""Gemini agent: tool'lar, executor, onay akışı, orchestrator, istemci. Gerçek API'ye ÇAĞRI YOK.

Orchestrator testleri FakeGeminiClient ile; GeminiClient (sarmalayıcı) testleri sahte bir SDK nesnesiyle.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from backend import services
from backend.agent.gemini_client import (
    ChatTurn,
    GeminiClient,
    GeminiError,
    QuotaExceededError,
)
from backend.agent.orchestrator import FactoryFlowAgent
from backend.agent.task_logger import mask_secrets
from backend.agent.tool_executor import ToolExecutor
from backend.agent.tool_registry import (
    TOOL_NAMES,
    TOOLS,
    ToolContext,
    function_declaration_dicts,
    function_declarations,
)
from backend.config import Settings, reset_settings_cache
from backend.database import configure_database
from backend.demo.demo_factory import build_demo_project
from backend.layout.editing import clone_layout, move_workstation
from backend.layout.validation import validate_layout
from backend.routing.engine import compute_flow_metrics, compute_routes, derive_flow_edges
from backend.schemas import (
    GeminiStatus,
    LayoutAlternative,
    OptimizationResult,
    ToolStatus,
)
from tests.fakes import FakeGeminiClient, Step

SECRET = "AIzaSyTESTSECRET1234567890abcdefghijk"

EXPECTED_TOOLS = [
    "create_project", "import_factory_layout", "add_workstation", "update_workstation", "validate_layout",
    "calculate_material_flow", "calculate_routes", "create_spaghetti_chart", "configure_simulation",
    "run_simpy_simulation", "get_simulation_metrics", "analyze_bottlenecks", "generate_layout_alternatives",
    "optimize_factory_layout", "compare_scenarios", "calculate_savings", "generate_engineering_report",
    "export_excel_report",
]


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    reset_settings_cache()
    configure_database(f"sqlite:///{tmp_path / 'agent.db'}")
    yield
    reset_settings_cache()


@pytest.fixture
def demo():
    return services.ensure_demo_project()


@pytest.fixture
def ex(tmp_path, demo):
    return ToolExecutor(ToolContext(project_id=demo.id, artifact_dir=tmp_path / "art"))


def _settings(**kw) -> Settings:
    base = dict(gemini_api_key=SECRET, gemini_model="test-model", external_data_consent=True,
                gemini_max_requests_per_task=10, _env_file=None)
    base.update(kw)
    return Settings(**base)


def _agent(ex, script, status=GeminiStatus.CONNECTED, **kw):
    fake = FakeGeminiClient(script, status=status)
    return FactoryFlowAgent(fake, ex, settings=kw.pop("settings", _settings()), **kw), fake


# --------------------------------------------------------------------------- registry
def test_registry_has_exactly_the_18_master_prompt_tools():
    assert list(TOOL_NAMES) == EXPECTED_TOOLS
    assert set(TOOLS) == set(EXPECTED_TOOLS)


def test_function_declarations_are_valid_and_ref_free():
    decls = function_declaration_dicts()
    assert [d["name"] for d in decls] == EXPECTED_TOOLS
    for d in decls:
        raw = json.dumps(d["parameters_json_schema"])
        assert "$ref" not in raw and "$defs" not in raw and d["description"]
        assert d["parameters_json_schema"]["type"] == "object"
    sdk = function_declarations()
    assert len(sdk) == 18 and sdk[0].name == "create_project"
    upd = next(d for d in decls if d["name"] == "update_workstation")["parameters_json_schema"]
    assert "station_id" in upd["required"]


# --------------------------------------------------------------------------- executor: doğrulama / reddetme
def test_unknown_and_disallowed_tools_are_rejected(ex, demo):
    r = ex.execute(ex.start_task("x"), "delete_everything", {})
    assert r.status == ToolStatus.REJECTED and r.error_code == "TOOL_NOT_ALLOWED"
    ex.allowed_tools = {"validate_layout"}
    r2 = ex.execute(ex.start_task("x"), "run_simpy_simulation", {})
    assert r2.status == ToolStatus.REJECTED and r2.error_code == "TOOL_NOT_ALLOWED"


@pytest.mark.parametrize("name,args", [
    ("update_workstation", {}),                                            # zorunlu alan yok
    ("update_workstation", {"station_id": "raw", "x": "abc"}),             # tip hatası
    ("update_workstation", {"station_id": "raw", "evil": 1}),              # fazla alan
    ("add_workstation", {"station_id": "n1", "name": "N", "x": 1, "y": 1, "width": -2, "length": 3}),
    ("calculate_material_flow", {"top_n": 0}),
    ("configure_simulation", {"replications": 0}),
    ("generate_layout_alternatives", {"algorithm": "magic"}),
])
def test_invalid_arguments_are_rejected_before_any_handler_runs(ex, name, args):
    r = ex.execute(ex.start_task("x"), name, args)
    assert r.status == ToolStatus.ERROR and r.error_code == "INVALID_ARGS" and r.error


def test_tool_call_limit_per_task(ex):
    ex.max_tool_calls = 2
    tid = ex.start_task("x")
    assert ex.execute(tid, "validate_layout", {}).status == ToolStatus.OK
    assert ex.execute(tid, "validate_layout", {}).status == ToolStatus.OK
    r = ex.execute(tid, "validate_layout", {})
    assert r.status == ToolStatus.ERROR and r.error_code == "TOOL_CALL_LIMIT"
    assert ex.execute(ex.start_task("y"), "validate_layout", {}).status == ToolStatus.OK  # yeni görev serbest


def test_no_active_project_is_clear_error(tmp_path):
    ex = ToolExecutor(ToolContext(artifact_dir=tmp_path))
    r = ex.execute(ex.start_task("x"), "validate_layout", {})
    assert r.status == ToolStatus.ERROR and r.error_code == "NO_PROJECT"


def test_unknown_project_is_not_found(ex):
    r = ex.execute(ex.start_task("x"), "validate_layout", {"project_id": "yok"})
    assert r.status == ToolStatus.ERROR and r.error_code == "NOT_FOUND"


# --------------------------------------------------------------------------- executor: gerçek domain tool'ları + kayıt
def test_read_only_tools_use_real_domain_modules_and_are_logged(ex, demo):
    tid = ex.start_task("analiz")
    v = ex.execute(tid, "validate_layout", {})
    assert v.status == ToolStatus.OK and v.data["valid"] is True
    f = ex.execute(tid, "calculate_material_flow", {"top_n": 3})
    edges = demo.flow_edges or derive_flow_edges(demo)
    expected = compute_flow_metrics(demo.layout, edges, compute_routes(demo.layout, edges), demo)
    assert f.status == ToolStatus.OK
    assert f.data["total_distance_m_per_hour"] == pytest.approx(expected.total_distance_m_per_hour)
    assert len(f.data["top_cells"]) == 3
    r = ex.execute(tid, "calculate_routes", {})
    assert r.status == ToolStatus.OK and r.data["n_routes"] > 0
    sp = ex.execute(tid, "create_spaghetti_chart", {})
    assert sp.status == ToolStatus.OK and sp.artifacts and sp.artifacts[0].endswith(".html")

    recs = ex.records(tid)
    assert [x.tool for x in recs] == ["validate_layout", "calculate_material_flow", "calculate_routes",
                                      "create_spaghetti_chart"]
    assert all(x.task_id == tid and x.duration_ms >= 0 and x.status == ToolStatus.OK for x in recs)
    stored = services.get_agent_task(tid)       # DB'ye de yazıldı
    assert len(stored["tool_calls"]) == 4 and stored["user_text"] == "analiz"


def test_simulation_chain_tools(ex):
    tid = ex.start_task("sim")
    assert ex.execute(tid, "get_simulation_metrics", {}).error_code == "NO_SIMULATION"
    cfg = ex.execute(tid, "configure_simulation", {"seed": 5, "replications": 1})
    assert cfg.status == ToolStatus.OK and cfg.data["persisted"] is False
    bad = ex.execute(tid, "configure_simulation", {"duration_s": 100, "warmup_s": 200})
    assert bad.status == ToolStatus.REJECTED
    run = ex.execute(tid, "run_simpy_simulation", {"scenario_name": "baseline"})
    assert run.status == ToolStatus.OK and run.data["seed"] == 5 and run.data["kpis"]["throughput_per_hour"] > 0
    m = ex.execute(tid, "get_simulation_metrics", {"run_id": run.data["run_id"]})
    assert m.data["kpis"] == run.data["kpis"]
    b = ex.execute(tid, "analyze_bottlenecks", {})
    assert b.status == ToolStatus.OK and "analysis" in b.data
    # kayıtlı proje sim ayarı değişmedi
    assert services.get_project(ex.ctx.project_id).sim_config.seed == build_demo_project().sim_config.seed


def test_create_project_does_not_overwrite_and_sets_active(ex, demo):
    tid = ex.start_task("yeni")
    r = ex.execute(tid, "create_project", {"name": "Test Fabrika", "project_id": "tf-1", "width_m": 30, "height_m": 20})
    assert r.status == ToolStatus.OK and ex.ctx.project_id == "tf-1" and r.data["n_workstations"] == 0
    again = ex.execute(tid, "create_project", {"name": "Başka", "project_id": "tf-1"})
    assert again.status == ToolStatus.ERROR and again.error_code == "PROJECT_EXISTS"
    cp = ex.execute(tid, "create_project", {"name": "Kopya", "from_demo": True})
    assert cp.status == ToolStatus.OK and cp.data["n_workstations"] == len(demo.layout.workstations)
    assert services.get_project(demo.id).layout.id == demo.layout.id    # orijinal demo korunur


# --------------------------------------------------------------------------- onay akışı
def _active_layout(pid):
    return services.get_layout(pid)


def test_layout_change_needs_approval_and_does_not_touch_saved_layout(ex, demo):
    tid = ex.start_task("taşı")
    before = _active_layout(demo.id)
    r = ex.execute(tid, "update_workstation", {"station_id": "raw", "x": 1, "y": 3})
    assert r.status == ToolStatus.NEEDS_APPROVAL and r.pending_change_id
    assert r.data["applied"] is False
    assert _active_layout(demo.id) == before                      # ONAYSIZ DEĞİŞMEZ
    assert len(services.list_layout_versions(demo.id)) == 1
    pend = ex.pending_changes(tid)
    assert len(pend) == 1 and pend[0].tool == "update_workstation" and pend[0].approved is None

    ok = ex.approve_change(r.pending_change_id)
    assert ok.status == ToolStatus.OK and ok.data["applied"] is True
    after = _active_layout(demo.id)
    assert after.station("raw").y == 3 and before.station("raw").y == 2
    assert after.parent_id == before.id and len(services.list_layout_versions(demo.id)) == 2
    assert ex.approve_change(r.pending_change_id).error_code == "ALREADY_RESOLVED"
    assert ex.pending_changes(tid) == []


def test_reject_change_keeps_layout(ex, demo):
    tid = ex.start_task("taşı")
    before = _active_layout(demo.id)
    r = ex.execute(tid, "update_workstation", {"station_id": "raw", "x": 2, "y": 2})
    rej = ex.reject_change(r.pending_change_id)
    assert rej.status == ToolStatus.REJECTED and rej.error_code == "REJECTED_BY_USER"
    assert _active_layout(demo.id) == before
    assert ex.approve_change(r.pending_change_id).error_code == "ALREADY_RESOLVED"   # reddedilen onaylanamaz
    assert ex.approve_change("chg-yok").error_code == "UNKNOWN_CHANGE"


def test_invalid_layout_change_is_rejected_not_queued(ex, demo):
    tid = ex.start_task("kötü")
    r = ex.execute(tid, "update_workstation", {"station_id": "raw", "x": 5, "y": 2})   # kolon/güvenlik boşluğu
    assert r.status == ToolStatus.REJECTED and r.error_code == "INVALID_LAYOUT" and r.data["issues"]
    assert ex.pending_changes() == []
    r2 = ex.execute(tid, "update_workstation", {"station_id": "olmayan", "x": 1})
    assert r2.status == ToolStatus.REJECTED and r2.error_code == "INVALID_CHANGE"
    dup = ex.execute(tid, "add_workstation", {"station_id": "raw", "name": "Kopya", "x": 20, "y": 6,
                                              "width": 2, "length": 2})
    assert dup.status == ToolStatus.REJECTED
    assert len(services.list_layout_versions(demo.id)) == 1


def test_add_workstation_and_import_require_approval(ex, demo):
    tid = ex.start_task("ekle")
    csv_text = services.export_workstations_csv(demo.layout)
    imp = ex.execute(tid, "import_factory_layout", {"format": "workstations_csv", "content": csv_text})
    assert imp.status == ToolStatus.NEEDS_APPROVAL
    bad = ex.execute(tid, "import_factory_layout", {"format": "workstations_csv", "content": "bozuk,csv\n1,2"})
    assert bad.status == ToolStatus.REJECTED
    add = ex.execute(tid, "add_workstation", {"station_id": "yeni", "name": "Yeni", "x": 20, "y": 6,
                                              "width": 2, "length": 2})
    assert add.status in (ToolStatus.NEEDS_APPROVAL, ToolStatus.REJECTED)   # geçerlilik layout kurallarına bağlı
    assert len(services.list_layout_versions(demo.id)) == 1


def test_stale_pending_change_is_not_applied(ex, demo):
    tid = ex.start_task("a")
    first = ex.execute(tid, "update_workstation", {"station_id": "raw", "x": 1, "y": 3})
    second = ex.execute(tid, "update_workstation", {"station_id": "raw", "x": 2, "y": 2})
    assert ex.approve_change(first.pending_change_id).status == ToolStatus.OK
    stale = ex.approve_change(second.pending_change_id)
    assert stale.status == ToolStatus.ERROR and stale.error_code == "STALE_CHANGE"
    assert _active_layout(demo.id).station("raw").y == 3


# --------------------------------------------------------------------------- optimizasyon: hazır olmayan modül
def test_optimization_module_not_ready_is_clear_error(ex, monkeypatch):
    import backend.optimization.engine as eng

    def _raise(*a, **k):
        raise NotImplementedError

    monkeypatch.setattr(eng, "optimize_layout", _raise)
    tid = ex.start_task("opt")
    for name in ("generate_layout_alternatives", "optimize_factory_layout"):
        r = ex.execute(tid, name, {})
        assert r.status == ToolStatus.ERROR and r.error_code == "MODULE_NOT_READY"
        assert not r.data
    assert ex.execute(tid, "compare_scenarios", {}).error_code == "NO_OPTIMIZATION"


def test_report_module_not_ready_is_clear_error(ex, monkeypatch):
    import backend.reporting.builder as rb

    def _raise(*a, **k):
        raise NotImplementedError

    monkeypatch.setattr(rb, "build_report_context", _raise)
    tid = ex.start_task("rapor")
    assert ex.execute(tid, "generate_engineering_report", {}).error_code == "NO_SIMULATION"
    assert ex.execute(tid, "run_simpy_simulation", {}).status == ToolStatus.OK
    for name in ("generate_engineering_report", "export_excel_report"):
        r = ex.execute(tid, name, {})
        assert r.status == ToolStatus.ERROR and r.error_code == "MODULE_NOT_READY"


def _test_only_optimize(project, config=None):
    """TEST-ONLY STUB: Opus'un optimizasyon motoru yerine, GERÇEK layout/routing modülleriyle bir alternatif kurar.
    Yalnız executor/onay akışını sınamak içindir; optimizasyon sonucu iddiası DEĞİLDİR."""
    from backend.schemas import OptimizationConfig

    cfg = config or OptimizationConfig()
    edges = project.flow_edges or derive_flow_edges(project)
    base_flow = compute_flow_metrics(project.layout, edges, compute_routes(project.layout, edges), project)
    alt_layout = clone_layout(move_workstation(project.layout, "raw", 1, 3), "alt-test-1", "Alt (test)")
    alt_flow = compute_flow_metrics(alt_layout, edges, compute_routes(alt_layout, edges), project)
    alt = LayoutAlternative(
        id="alt-1", name="Alt 1", algorithm="test_only", layout=alt_layout, validation=validate_layout(alt_layout),
        objective=alt_flow.total_distance_m_per_hour, flow=alt_flow, sim=None,
        distance_reduction_pct=(base_flow.total_distance_m_per_hour - alt_flow.total_distance_m_per_hour)
        / base_flow.total_distance_m_per_hour * 100.0, moved_station_ids=["raw"])
    return OptimizationResult(baseline_layout_id=project.layout.id, baseline_flow=base_flow, alternatives=[alt],
                              best_alternative_id="alt-1", rationale="TEST-ONLY", config=cfg)


def test_optimize_apply_best_goes_through_approval_TEST_ONLY_STUB(ex, demo, monkeypatch):
    import backend.optimization.engine as eng

    monkeypatch.setattr(eng, "optimize_layout", _test_only_optimize)
    tid = ex.start_task("opt")
    before = _active_layout(demo.id)
    plain = ex.execute(tid, "optimize_factory_layout", {"simulate_alternatives": False})
    assert plain.status == ToolStatus.OK and plain.data["applied"] is False
    assert _active_layout(demo.id) == before
    r = ex.execute(tid, "optimize_factory_layout", {"apply_best": True, "simulate_alternatives": False})
    assert r.status == ToolStatus.NEEDS_APPROVAL and r.data["best_alternative_id"] == "alt-1"
    assert _active_layout(demo.id) == before                      # onaysız değişmez
    res = ex.approve_change(r.pending_change_id)
    assert res.status == ToolStatus.OK and res.data["alternative_id"] == "alt-1"
    assert _active_layout(demo.id).station("raw").y == 3
    # simülasyonsuz alternatif karşılaştırılamaz: sahte sayı üretilmez
    assert ex.execute(tid, "compare_scenarios", {}).error_code == "NO_SIMULATION"


# --------------------------------------------------------------------------- orchestrator (FakeGeminiClient)
def test_multi_step_chain_with_fake_gemini(ex, demo):
    agent, fake = _agent(ex, [
        Step.call("validate_layout", {}),
        Step.call("calculate_material_flow", {"top_n": 2}, ("run_simpy_simulation", {})),
        Step.call("analyze_bottlenecks", {}),
        Step.text("Layout geçerli; darboğaz analizi tamamlandı."),
    ])
    reply = agent.handle("Layout'u doğrula, akışı ve simülasyonu çalıştır, darboğazı bul.", demo.id)
    assert reply.text.startswith("Layout geçerli")
    assert [t.tool for t in reply.tool_calls] == ["validate_layout", "calculate_material_flow",
                                                 "run_simpy_simulation", "analyze_bottlenecks"]
    assert all(t.status == ToolStatus.OK for t in reply.tool_calls)
    assert reply.requests_used == 4 and reply.tokens_used == 40 and reply.gemini_status == GeminiStatus.CONNECTED
    # tool sonuçları Gemini'ye geri beslendi; sistem istemi Türkçe ve 18 tool bildirildi
    assert [t.role for t in fake.calls[-1]["turns"]][-2:] == ["model", "tool"]
    tool_turn = next(t for t in fake.calls[1]["turns"] if t.role == "tool")
    assert tool_turn.function_results[0].response["status"] == "ok"
    assert fake.calls[0]["tool_names"] == EXPECTED_TOOLS and "Türkçe" in fake.calls[0]["system_prompt"]
    assert services.get_agent_task(reply.task_id)["status"] == "done"


def test_agent_surfaces_pending_changes_and_never_claims_applied(ex, demo):
    agent, _ = _agent(ex, [Step.call("update_workstation", {"station_id": "raw", "x": 1, "y": 3}),
                           Step.text("raw istasyonunu taşımayı önerdim.")])
    before = _active_layout(demo.id)
    reply = agent.handle("raw istasyonunu yukarı al", demo.id)
    assert len(reply.pending_changes) == 1 and reply.pending_changes[0].id in reply.text
    assert "UYGULANMADI" in reply.text and _active_layout(demo.id) == before
    assert agent.approve_change(reply.pending_changes[0].id).status == ToolStatus.OK
    assert _active_layout(demo.id) != before


def test_gemini_cannot_call_approval_as_a_tool(ex, demo):
    agent, _ = _agent(ex, [Step.call("update_workstation", {"station_id": "raw", "x": 1, "y": 3}),
                           Step.call("approve_change", {"change_id": "x"}), Step.text("bitti")])
    before = _active_layout(demo.id)
    reply = agent.handle("taşı ve onayla", demo.id)
    assert reply.tool_calls[1].status == ToolStatus.REJECTED
    assert _active_layout(demo.id) == before


def test_max_requests_per_task_is_enforced(ex, demo):
    agent, fake = _agent(ex, [Step.call("validate_layout", {})] * 10, max_requests_per_task=3)
    reply = agent.handle("döngü", demo.id)
    assert fake.requests_used == 3 and len(reply.tool_calls) == 3
    assert "istek sınırına" in reply.text


def test_tool_call_limit_in_agent_loop(tmp_path, demo):
    ex = ToolExecutor(ToolContext(project_id=demo.id, artifact_dir=tmp_path), max_tool_calls=2)
    agent, _ = _agent(ex, [Step.call("validate_layout", {}), Step.call("validate_layout", {}),
                           Step.call("validate_layout", {}), Step.text("tamam")])
    reply = agent.handle("tekrar", demo.id)
    assert [t.status for t in reply.tool_calls] == [ToolStatus.OK, ToolStatus.OK, ToolStatus.ERROR]
    assert reply.tool_calls[2].error and "en fazla 2" in reply.tool_calls[2].error


def test_no_consent_sends_nothing_to_gemini_and_informs_user(ex, demo):
    agent, fake = _agent(ex, [Step.text("gönderilmemeli")], settings=_settings(external_data_consent=False))
    reply = agent.handle("Layout'u yorumla", demo.id)
    assert fake.calls == [] and fake.requests_used == 0
    assert "onay" in reply.text and "yerelde" in reply.text


def test_offline_analysis_without_consent_runs_deterministic_pipeline(ex, demo):
    agent, fake = _agent(ex, [], settings=_settings(external_data_consent=False))
    reply = agent.handle("Bu tesisi analiz et", demo.id)
    assert fake.calls == []
    edges = demo.flow_edges or derive_flow_edges(demo)
    flow = compute_flow_metrics(demo.layout, edges, compute_routes(demo.layout, edges), demo)
    assert f"{flow.total_distance_m_per_hour:.1f} m/saat" in reply.text      # sayı nesneden
    assert "GEÇERLİ" in reply.text and "throughput" in reply.text
    assert any(t.tool == "analyze_facility" for t in reply.tool_calls)
    assert reply.artifacts and reply.artifacts[0].endswith(".html")


@pytest.mark.parametrize("status", [GeminiStatus.API_KEY_MISSING, GeminiStatus.QUOTA_EXCEEDED,
                                    GeminiStatus.DISABLED, GeminiStatus.INVALID_MODEL])
def test_offline_mode_when_gemini_unavailable(ex, demo, status):
    agent, fake = _agent(ex, [], status=status)
    reply = agent.handle("Bu fabrikayı analiz et", demo.id)
    assert reply.gemini_status == status and fake.calls == []
    assert "devre dışı" in reply.text and "m/saat" in reply.text
    other = agent.handle("Bir şey sor", demo.id)
    assert "Offline modda" in other.text


def test_quota_error_midway_locks_ai_and_falls_back(ex, demo):
    agent, _ = _agent(ex, [Step.call("validate_layout", {}), Step.fail(QuotaExceededError())])
    reply = agent.handle("Bu tesisi analiz et", demo.id)
    assert reply.gemini_status == GeminiStatus.QUOTA_EXCEEDED and "kota" in reply.text.lower()
    assert "m/saat" in reply.text         # mühendislik yolu çalışmaya devam eder


def test_no_api_key_real_client_never_touches_network(ex, demo):
    class Boom:
        def __getattr__(self, n):
            raise AssertionError("SDK'ya dokunulmamalı")

    client = GeminiClient(_settings(gemini_api_key=None), sdk_client=Boom())
    assert client.status == GeminiStatus.API_KEY_MISSING
    agent = FactoryFlowAgent(client, ex, settings=_settings(gemini_api_key=None))
    reply = agent.handle("Bu tesisi analiz et", demo.id)
    assert reply.gemini_status == GeminiStatus.API_KEY_MISSING and "m/saat" in reply.text


# --------------------------------------------------------------------------- GeminiClient (sahte SDK)
class _Api(Exception):
    def __init__(self, code, msg="x"):
        super().__init__(f"{code} {msg}")
        self.code = code


class _Sdk:
    """Sahte google-genai istemcisi: models.generate_content / get / list."""

    def __init__(self, behaviours):
        self.behaviours = list(behaviours)
        self.n = 0
        self.models = SimpleNamespace(generate_content=self._gen, get=self._get, list=self._list)

    def _gen(self, model, contents, config):
        self.n += 1
        b = self.behaviours.pop(0) if self.behaviours else "ok"
        if isinstance(b, Exception):
            raise b
        part = SimpleNamespace(text="merhaba", function_call=None, thought=False)
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))],
                               usage_metadata=SimpleNamespace(total_token_count=7))

    def _get(self, model):
        if model == "yok-model":
            raise _Api(404, "model not found")
        return SimpleNamespace(name=model)

    def _list(self):
        return [SimpleNamespace(name="models/a-model", display_name="A", supported_actions=["generateContent"]),
                SimpleNamespace(name="models/emb", display_name="E", supported_actions=["embedContent"])]


def _gc(behaviours, **kw):
    sleeps: list[float] = []
    sdk = _Sdk(behaviours)
    c = GeminiClient(kw.pop("settings", _settings()), sdk_client=sdk, sleep=sleeps.append, **kw)
    return c, sdk, sleeps


TURN = [ChatTurn(role="user", text="selam")]


def test_client_counts_requests_tokens_and_caches():
    c, sdk, _ = _gc([])
    r1 = c.generate(TURN, system_prompt="s", tools=[])
    assert r1.text == "merhaba" and c.requests_used == 1 and c.tokens_used == 7 and c.verified
    r2 = c.generate(TURN, system_prompt="s", tools=[])
    assert r2.cached and sdk.n == 1 and c.requests_used == 1       # önbellek: yeni istek yok
    c.generate([ChatTurn(role="user", text="başka")], system_prompt="s", tools=[])
    assert sdk.n == 2 and c.requests_used == 2


def test_client_exponential_backoff_on_transient_errors():
    c, sdk, sleeps = _gc([_Api(503), _Api(503), "ok"])
    assert c.generate(TURN, system_prompt="s", tools=[]).text == "merhaba"
    assert sleeps == [1.0, 2.0] and sdk.n == 3 and c.status == GeminiStatus.CONNECTED


def test_client_gives_up_after_retries_with_connection_error():
    c, sdk, sleeps = _gc([_Api(503)] * 10, max_retries=2)
    with pytest.raises(GeminiError) as ei:
        c.generate(TURN, system_prompt="s", tools=[])
    assert ei.value.status == GeminiStatus.CONNECTION_ERROR and sdk.n == 3 and sleeps == [1.0, 2.0]


def test_client_quota_error_locks_and_blocks_further_calls():
    c, sdk, _ = _gc([_Api(429, "RESOURCE_EXHAUSTED")] * 10, max_retries=1)
    with pytest.raises(QuotaExceededError):
        c.generate(TURN, system_prompt="s", tools=[])
    assert c.status == GeminiStatus.QUOTA_EXCEEDED
    n = sdk.n
    with pytest.raises(QuotaExceededError):
        c.generate([ChatTurn(role="user", text="yine")], system_prompt="s", tools=[])
    assert sdk.n == n                                                # kilit: ağa gidilmedi
    c.unlock_quota()
    assert c.status == GeminiStatus.CONNECTED


def test_client_daily_budget_locks():
    c, sdk, _ = _gc([], settings=_settings(gemini_daily_request_budget=2))
    for i in range(2):
        c.generate([ChatTurn(role="user", text=f"m{i}")], system_prompt="s", tools=[])
    with pytest.raises(QuotaExceededError):
        c.generate([ChatTurn(role="user", text="m3")], system_prompt="s", tools=[])
    assert sdk.n == 2 and c.status == GeminiStatus.QUOTA_EXCEEDED


def test_client_disabled_or_keyless_makes_no_calls():
    for kw, st in [({"gemini_api_key": None}, GeminiStatus.API_KEY_MISSING),
                   ({"gemini_enabled": False}, GeminiStatus.DISABLED),
                   ({"gemini_model": ""}, GeminiStatus.INVALID_MODEL)]:
        c, sdk, _ = _gc([], settings=_settings(**kw))
        assert c.status == st
        with pytest.raises(GeminiError):
            c.generate(TURN, system_prompt="s", tools=[])
        assert sdk.n == 0


def test_client_model_verification_listing_and_manual_switch_no_auto_fallback():
    c, sdk, _ = _gc([])
    assert c.verify_model()[0] is True
    assert [m["name"] for m in c.list_available_models()] == ["a-model"]
    ok, _ = c.select_model("yok-model")
    assert ok is False and c.model == "test-model"                   # geçersiz model uygulanmaz
    assert c.select_model("a-model")[0] is True and c.model == "a-model"
    bad, _, _ = _gc([_Api(404, "model not found")])
    with pytest.raises(GeminiError):
        bad.generate(TURN, system_prompt="s", tools=[])
    assert bad.status == GeminiStatus.INVALID_MODEL and bad.model == "test-model"   # otomatik model değişimi yok


# --------------------------------------------------------------------------- sır sızıntısı
def test_api_key_never_leaks(ex, demo, caplog, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    reset_settings_cache()
    caplog.set_level(logging.DEBUG)
    # 1) istemci: repr, hata mesajı, log
    c, _, _ = _gc([_Api(500, f"upstream failed key={SECRET}")] * 5, max_retries=1)
    assert SECRET not in repr(c) and SECRET not in str(c.status)
    with pytest.raises(GeminiError) as ei:
        c.generate(TURN, system_prompt="s", tools=[])
    assert SECRET not in str(ei.value) and SECRET not in ei.value.message
    # 2) agent: kullanıcı ve model sırrı yazsa bile kayıt/yanıt/DB temiz
    agent, _ = _agent(ex, [Step.call("run_simpy_simulation", {"scenario_name": f"sc {SECRET}"}),
                           Step.text(f"Bitti, anahtar {SECRET}")])
    reply = agent.handle(f"Simüle et. Anahtarım {SECRET}", demo.id)
    blob = json.dumps(reply.model_dump(mode="json"), ensure_ascii=False)
    blob += json.dumps(services.get_agent_task(reply.task_id), ensure_ascii=False)
    blob += caplog.text
    assert SECRET not in blob
    assert "***" in blob
    # 3) maskeleme yardımcıları
    assert SECRET not in json.dumps(mask_secrets({"api_key": SECRET, "x": ["Bearer abcdefgh12345", SECRET]}))
    assert mask_secrets({"gemini_api_key": "abc"})["gemini_api_key"] == "***"
