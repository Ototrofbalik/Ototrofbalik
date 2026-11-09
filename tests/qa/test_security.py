"""[QA] Kontrol 7 — güvenlik: onaysız layout değişimi yok, Gemini onay veremez, tool sınırı, parametre doğrulama,
sır sızıntısı taraması, .env git dışı, model adı sabit değil, harici veri onayı."""
from __future__ import annotations

import logging
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from backend import services
from backend.agent.orchestrator import FactoryFlowAgent
from backend.agent.tool_executor import ToolExecutor
from backend.agent.tool_registry import TOOL_NAMES, TOOLS, ToolContext, function_declaration_dicts
from backend.config import Settings, reset_settings_cache
from backend.schemas import ToolStatus
from tests.fakes import FakeGeminiClient, Step
from tests.qa.conftest import REPO

# Gerçek görünümlü ama SAHTE anahtar: kaynakta literal olarak geçmemesi için parçalı kurulur.
FAKE_GOOGLE_KEY = "AI" + "zaSy" + "QA0123456789abcdefghijklmnopqrstu"
FAKE_PLAIN_KEY = "qa-" + "plain-secret-value-9876543210"


def _ex(tmp_path, **kw):
    ex = ToolExecutor(ToolContext(project_id="demo-project", artifact_dir=tmp_path), **kw)
    return ex, ex.start_task("qa", "demo-project")


def _snapshot():
    return services.get_layout("demo-project").model_dump(), services.list_layout_versions("demo-project")


# ----------------------------------------------------------------------------------------- onaysız değişim
def _moved_layout_json():
    lay = services.get_layout("demo-project")
    ws = [w.model_copy(update={"y": 2.3}) if w.id == "raw" else w for w in lay.workstations]
    return lay.model_copy(update={"workstations": ws, "id": "imported-v2", "name": "içe aktarılan"}).model_dump_json()


MUTATING = [
    ("update_workstation", {"station_id": "raw", "y": 2.3}),
    ("add_workstation", {"station_id": "new1", "name": "Yeni", "x": 13.6, "y": 2, "width": 2.8, "length": 3}),
    ("import_factory_layout", "JSON"),
    ("optimize_factory_layout", {"apply_best": True, "iterations": 30, "simulate_alternatives": False}),
]


@pytest.mark.parametrize("name,args", MUTATING, ids=[m[0] for m in MUTATING])
def test_mutating_tools_need_approval_and_do_not_touch_saved_layout(db, tmp_path, name, args):
    if args == "JSON":
        args = {"format": "layout_json", "content": _moved_layout_json()}
    before = _snapshot()
    ex, tid = _ex(tmp_path)
    r = ex.execute(tid, name, args)
    assert r.status == ToolStatus.NEEDS_APPROVAL and r.pending_change_id, (r.status, r.error)
    assert r.data["applied"] is False
    assert _snapshot() == before                                  # kayıtlı layout DEĞİŞMEDİ
    assert len(ex.pending_changes(tid)) == 1
    # reddet -> hâlâ değişmez, tekrar onaylanamaz
    rej = ex.reject_change(r.pending_change_id)
    assert rej.status == ToolStatus.REJECTED and _snapshot() == before
    assert ex.approve_change(r.pending_change_id).error_code == "ALREADY_RESOLVED"
    assert _snapshot() == before


@pytest.mark.parametrize("name,args", MUTATING, ids=[m[0] for m in MUTATING])
def test_approval_is_the_only_path_that_commits(db, tmp_path, name, args):
    if args == "JSON":
        args = {"format": "layout_json", "content": _moved_layout_json()}
    ex, tid = _ex(tmp_path)
    r = ex.execute(tid, name, args)
    res = ex.approve_change(r.pending_change_id)
    assert res.status == ToolStatus.OK and res.data["applied"] is True
    versions = services.list_layout_versions("demo-project")
    assert len(versions) == 2                                      # eski sürüm KORUNDU, yenisi eklendi
    assert services.get_layout("demo-project").id == res.data["layout_id"]


def test_stale_approval_is_refused(db, tmp_path):
    ex, tid = _ex(tmp_path)
    p1 = ex.execute(tid, "update_workstation", {"station_id": "raw", "y": 2.3})
    p2 = ex.execute(tid, "update_workstation", {"station_id": "cut", "y": 14.3})
    assert ex.approve_change(p1.pending_change_id).status == ToolStatus.OK
    stale = ex.approve_change(p2.pending_change_id)
    assert stale.error_code == "STALE_CHANGE" and len(services.list_layout_versions("demo-project")) == 2


def test_gemini_cannot_approve_or_reject(db, tmp_path):
    names = {d["name"] for d in function_declaration_dicts()}
    assert not any(re.search(r"approve|reject|confirm|commit|apply|delete|drop|shell|exec", n) for n in names), names
    assert len(names) == 18 and names == set(TOOL_NAMES)
    ex, tid = _ex(tmp_path)
    pend = ex.execute(tid, "update_workstation", {"station_id": "raw", "y": 2.3})
    before = _snapshot()
    for bad in ("approve_change", "reject_change", "apply_change", "confirm_change", "commit"):
        r = ex.execute(tid, bad, {"change_id": pend.pending_change_id})
        assert r.status == ToolStatus.REJECTED and r.error_code == "TOOL_NOT_ALLOWED", bad
    # argüman kaçırma: onayı argümanla taklit etme
    r = ex.execute(tid, "update_workstation", {"station_id": "raw", "y": 2.3, "approved": True})
    assert r.error_code == "INVALID_ARGS"
    r = ex.execute(tid, "update_workstation", {"station_id": "raw", "y": 2.3, "change_id": pend.pending_change_id})
    assert r.error_code == "INVALID_ARGS"
    assert _snapshot() == before and ex.pending_changes(tid)[0].approved is None


def test_fake_gemini_trying_to_approve_via_conversation_fails(db, tmp_path):
    """Model önce öneri üretir, sonra 'approve_change' çağırmayı dener: layout değişmemeli."""
    before = _snapshot()
    holder = {}

    def second(turns):
        pid = [fr.response.get("pending_change_id") for t in turns for fr in t.function_results
               if fr.name == "update_workstation"][0]
        holder["pid"] = pid
        return Step.call("approve_change", {"change_id": pid})

    client = FakeGeminiClient([Step.call("update_workstation", {"station_id": "raw", "y": 2.3}), second,
                               Step.text("Onayladım.")])
    agent = FactoryFlowAgent(client, ToolExecutor(ToolContext(artifact_dir=tmp_path)),
                             settings=Settings(external_data_consent=True))
    reply = agent.handle("raw istasyonunu taşı ve onayla", "demo-project")
    assert [r.status for r in reply.tool_calls] == [ToolStatus.NEEDS_APPROVAL, ToolStatus.REJECTED]
    assert reply.tool_calls[1].error is not None and "izin verilen" in reply.tool_calls[1].error
    assert _snapshot() == before
    assert len(reply.pending_changes) == 1 and reply.pending_changes[0].approved is None
    assert "UYGULANMADI" in reply.text


# ----------------------------------------------------------------------------------------- limitler
def test_executor_max_tool_call_limit(db, tmp_path):
    ex, tid = _ex(tmp_path, max_tool_calls=3)
    out = [ex.execute(tid, "validate_layout", {}) for _ in range(5)]
    assert [r.status for r in out[:3]] == [ToolStatus.OK] * 3
    assert all(r.error_code == "TOOL_CALL_LIMIT" for r in out[3:])
    # yeni görev yeni sayaçla başlar
    tid2 = ex.start_task("yeni", "demo-project")
    assert ex.execute(tid2, "validate_layout", {}).status == ToolStatus.OK


def test_orchestrator_request_limit_stops_infinite_tool_loop(db, tmp_path):
    client = FakeGeminiClient([Step.call("validate_layout", {}) for _ in range(20)])
    agent = FactoryFlowAgent(client, ToolExecutor(ToolContext(artifact_dir=tmp_path)), max_requests_per_task=3,
                             settings=Settings(external_data_consent=True))
    reply = agent.handle("sonsuz döngü", "demo-project")
    assert reply.requests_used == 3 and len(client.script) == 17        # yalnız 3 istek tüketildi
    assert "istek sınırına (3) ulaşıldı" in reply.text and len(reply.tool_calls) == 3


BAD_ARGS = [
    ("calculate_material_flow", {"top_n": -1}), ("calculate_material_flow", {"top_n": 0}),
    ("calculate_material_flow", {"top_n": 9999}), ("calculate_material_flow", {"top_n": "abc"}),
    ("calculate_routes", {"mode": "teleport"}),
    ("update_workstation", {"station_id": "raw", "rotation": "45"}),
    ("update_workstation", {"station_id": "raw", "width": -3}),
    ("update_workstation", {"station_id": ""}), ("update_workstation", {}),
    ("update_workstation", {"station_id": "raw", "x": "yüz"}),
    ("add_workstation", {"station_id": "z", "name": "z", "x": 0, "y": 0, "width": 0, "length": 3}),
    ("add_workstation", {"station_id": "z", "name": "z", "x": 0, "y": 0, "width": 3, "length": 3, "capacity": 0}),
    ("configure_simulation", {"replications": 0}), ("configure_simulation", {"duration_s": -5}),
    ("configure_simulation", {"seed": "x"}), ("configure_simulation", {"arrival": "magic"}),
    ("configure_simulation", {"resources": [{"vehicle": "rocket", "count": 1, "speed_mps": 1}]}),
    ("configure_simulation", {"resources": [{"vehicle": "operator", "count": 1, "speed_mps": 0}]}),
    ("generate_layout_alternatives", {"iterations": 0}), ("generate_layout_alternatives", {"iterations": 10**9}),
    ("generate_layout_alternatives", {"n_alternatives": 99}), ("generate_layout_alternatives", {"algorithm": "rm -rf"}),
    ("generate_layout_alternatives", {"target_distance_reduction_pct": 150}),
    ("generate_layout_alternatives", {"target_distance_reduction_pct": 0}),
    ("optimize_factory_layout", {"apply_best": "evet-belki"}),
    ("generate_engineering_report", {"format": "docx"}), ("generate_engineering_report", {"ai_summary": "x" * 5000}),
    ("create_project", {"name": ""}), ("create_project", {"name": "x", "project_id": "../etc/passwd"}),
    ("create_project", {"name": "x", "width_m": -1}), ("create_project", {"name": "x" * 500}),
    ("import_factory_layout", {"format": "exe", "content": "xx"}), ("import_factory_layout", {"format": "layout_json"}),
    ("validate_layout", {"unknown_param": 1}),
]


@pytest.mark.parametrize("name,args", BAD_ARGS, ids=[f"{n}:{i}" for i, (n, _) in enumerate(BAD_ARGS)])
def test_invalid_parameters_are_rejected_before_any_effect(db, tmp_path, name, args):
    before = _snapshot()
    ex, tid = _ex(tmp_path)
    r = ex.execute(tid, name, args)
    assert r.status == ToolStatus.ERROR and r.error_code == "INVALID_ARGS", (r.status, r.error_code, r.error)
    assert _snapshot() == before and not ex.pending_changes(tid)
    assert len(services.list_projects()) == 1


@pytest.mark.parametrize("name", ["os.system", "", "__import__", "VALIDATE_LAYOUT", "validate_layout ", "../tool"])
def test_unknown_tool_names_rejected(db, tmp_path, name):
    ex, tid = _ex(tmp_path)
    r = ex.execute(tid, name, {})
    assert r.status == ToolStatus.REJECTED and r.error_code == "TOOL_NOT_ALLOWED"


def test_physically_invalid_layout_changes_are_rejected(db, tmp_path):
    ex, tid = _ex(tmp_path)
    cases = {
        "dışarı": ("update_workstation", {"station_id": "raw", "x": 100, "y": 100}),
        "çakışma": ("update_workstation", {"station_id": "raw", "x": 12, "y": 14}),          # qc ile çakışır
        "engel": ("update_workstation", {"station_id": "raw", "x": 6.5, "y": 3.0}),          # kolon üstü
        "yinelenen id": ("add_workstation", {"station_id": "raw", "name": "kopya", "x": 13.6, "y": 2,
                                             "width": 2.8, "length": 3}),
        "bilinmeyen istasyon": ("update_workstation", {"station_id": "yok", "x": 1}),
    }
    before = _snapshot()
    for label, (n, a) in cases.items():
        r = ex.execute(tid, n, a)
        assert r.status == ToolStatus.REJECTED and r.error_code in ("INVALID_LAYOUT", "INVALID_CHANGE"), \
            (label, r.status, r.error_code, r.error)
    assert _snapshot() == before and not ex.pending_changes(tid)


def test_fixed_station_cannot_be_moved_through_tool(db, tmp_path):
    p = services.get_project("demo-project")
    lay = p.layout.model_copy(deep=True)
    lay.workstations = [w.model_copy(update={"fixed": w.id == "raw"}) for w in lay.workstations]
    services.save_project(p.model_copy(update={"layout": lay}))
    ex, tid = _ex(tmp_path)
    r = ex.execute(tid, "update_workstation", {"station_id": "raw", "y": 2.3})
    assert r.status == ToolStatus.REJECTED and "sabit" in (r.error or "")


def test_unknown_project_is_clean_error_not_crash(db, tmp_path):
    ex, tid = _ex(tmp_path)
    r = ex.execute(tid, "validate_layout", {"project_id": "yok-boyle-proje"})
    assert r.status == ToolStatus.ERROR and r.error_code in ("NOT_FOUND", "SERVICE_ERROR")


# ----------------------------------------------------------------------------------------- harici veri onayı
def test_no_data_goes_to_gemini_without_consent(db, tmp_path):
    client = FakeGeminiClient([Step.text("GİTMEMELİ")])
    agent = FactoryFlowAgent(client, ToolExecutor(ToolContext(artifact_dir=tmp_path)),
                             settings=Settings(external_data_consent=False))
    for prompt in ("Bu tesisi analiz et", "raw istasyonunu taşı", "merhaba"):
        reply = agent.handle(prompt, "demo-project")
        assert "onayınız yok" in reply.text and reply.requests_used == 0
    assert client.calls == [] and len(client.script) == 1                  # fake'e HİÇ çağrı gitmedi


def test_with_consent_data_is_sent_control(db, tmp_path):
    client = FakeGeminiClient([Step.text("tamam")])
    agent = FactoryFlowAgent(client, ToolExecutor(ToolContext(artifact_dir=tmp_path)),
                             settings=Settings(external_data_consent=True))
    reply = agent.handle("merhaba", "demo-project")
    assert len(client.calls) == 1 and reply.text == "tamam"


def test_consent_default_is_false_and_settings_hide_secret(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_GOOGLE_KEY)
    reset_settings_cache()
    s = Settings()
    assert s.external_data_consent is False
    for rep in (repr(s), str(s), s.model_dump_json(), str(s.model_dump())):
        assert FAKE_GOOGLE_KEY not in rep
    from backend.agent.gemini_client import GeminiClient

    c = GeminiClient(s)
    assert FAKE_GOOGLE_KEY not in repr(c) and FAKE_GOOGLE_KEY not in str(c.__dict__.get("_settings", ""))


def test_gemini_model_not_hardcoded_and_empty_model_is_invalid(monkeypatch):
    assert Settings().gemini_model == ""
    pat = re.compile(r"gemini[-_ ]?(\d|pro|flash|ultra|nano)|models/gemini", re.I)
    hits = []
    for d in ("backend", "frontend"):
        for p in (REPO / d).rglob("*.py"):
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if pat.search(line):
                    hits.append(f"{p.relative_to(REPO)}:{i}: {line.strip()[:80]}")
    assert hits == [], hits
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_PLAIN_KEY)
    reset_settings_cache()
    from backend.agent.gemini_client import GeminiClient
    from backend.schemas import GeminiStatus

    assert GeminiClient(Settings()).status == GeminiStatus.INVALID_MODEL     # model .env'den gelmeli


# ----------------------------------------------------------------------------------------- sır sızıntısı
def test_secret_never_leaks_to_logs_db_reports_or_replies(monkeypatch, caplog, db, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_PLAIN_KEY)   # desen dışı -> yalnız 'yapılandırılmış sır' maskelemesi
    reset_settings_cache()
    from backend.agent.gemini_client import GeminiError
    from backend.schemas import GeminiStatus

    caplog.set_level(logging.DEBUG)
    leaky = [
        Step.call("validate_layout", {"layout_id": FAKE_PLAIN_KEY}, ("update_workstation", {
            "station_id": FAKE_GOOGLE_KEY, "y": 1})),
        Step.call("run_simpy_simulation", {}),
        Step.call("generate_engineering_report", {"format": "html", "ai_summary": "Darboğaz drill istasyonudur."}),
        Step.fail(GeminiError(GeminiStatus.CONNECTION_ERROR, f"401 key={FAKE_GOOGLE_KEY} Bearer {FAKE_PLAIN_KEY}")),
    ]
    client = FakeGeminiClient(leaky)
    s = Settings(external_data_consent=True)
    assert s.gemini_api_key is not None and s.gemini_api_key.get_secret_value() == FAKE_PLAIN_KEY
    agent = FactoryFlowAgent(client, ToolExecutor(ToolContext(artifact_dir=tmp_path / "art")), settings=s)
    reply = agent.handle(f"Benim anahtarım {FAKE_GOOGLE_KEY} ve {FAKE_PLAIN_KEY}; tesisi analiz et", "demo-project")
    haystacks = {
        "reply.text": reply.text,
        "tool_calls": " ".join(f"{r.args} {r.output_summary} {r.error}" for r in reply.tool_calls),
        "log": caplog.text,
        "task": str(services.get_agent_task(reply.task_id)),
        "tasks": str(services.list_agent_tasks()),
    }
    for p in (tmp_path / "art").rglob("*"):
        if p.is_file():
            haystacks[f"artifact:{p.name}"] = p.read_bytes().decode("utf-8", "ignore")
    con = sqlite3.connect(db)
    haystacks["db"] = "\n".join(str(r) for t in ("agent_tasks",) for r in con.execute(f"select * from {t}"))
    con.close()
    raw_db = db.read_bytes()
    for key in (FAKE_GOOGLE_KEY, FAKE_PLAIN_KEY):
        for name, h in haystacks.items():
            assert key not in h, f"{name} anahtar sızdırdı"
        assert key.encode() not in raw_db, "ham DB dosyasında anahtar var"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F-02: ai_summary rapora maskelenmeden yazılıyor (sır içeren metin rapor dosyasına sızar)")
def test_secret_in_ai_summary_is_masked_in_report_file(monkeypatch, db, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_PLAIN_KEY)
    reset_settings_cache()
    ex = ToolExecutor(ToolContext(project_id="demo-project", artifact_dir=tmp_path / "art"))
    tid = ex.start_task("x", "demo-project")
    assert ex.execute(tid, "run_simpy_simulation", {}).status == ToolStatus.OK
    r = ex.execute(tid, "generate_engineering_report",
                   {"format": "html", "ai_summary": f"anahtar {FAKE_PLAIN_KEY} ve {FAKE_GOOGLE_KEY}"})
    assert r.status == ToolStatus.OK
    html = Path(r.artifacts[0]).read_text(encoding="utf-8")
    assert FAKE_PLAIN_KEY not in html and FAKE_GOOGLE_KEY not in html


def test_secret_not_in_offline_report_pipeline_outputs(monkeypatch, db, tmp_path, caplog):
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_GOOGLE_KEY)
    reset_settings_cache()
    caplog.set_level(logging.DEBUG)
    agent = FactoryFlowAgent(None, ToolExecutor(ToolContext(artifact_dir=tmp_path / "a")),
                             settings=Settings(external_data_consent=False))
    reply = agent.handle("Bu tesisi analiz et", "demo-project")
    assert FAKE_GOOGLE_KEY not in reply.text and FAKE_GOOGLE_KEY not in caplog.text
    for p in (tmp_path / "a").rglob("*"):
        if p.is_file():
            assert FAKE_GOOGLE_KEY.encode() not in p.read_bytes()


def _git(*a):
    return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True)


def test_env_file_not_tracked_and_ignored():
    tracked = _git("ls-files").stdout.splitlines()
    assert ".env" not in tracked and not any(t.endswith("/.env") for t in tracked)
    assert _git("check-ignore", "-q", ".env").returncode == 0
    ex = (REPO / ".env.example").read_text(encoding="utf-8")
    assert "YOUR_API_KEY_HERE" in ex and not re.search(r"AIza[0-9A-Za-z_\-]{20,}", ex)
    gi = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in gi and any(g.startswith("data/*.db") for g in gi)


def test_no_google_key_pattern_in_tracked_files_or_history():
    pat = re.compile(r"AIza[0-9A-Za-z_\-]{35}")
    leaks = []
    for f in _git("ls-files").stdout.splitlines():
        p = REPO / f
        if p.suffix in (".png", ".db", ".xlsx", ".pdf", ".ico") or not p.is_file():
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if pat.search(txt):
            leaks.append(f)
    assert leaks == [], leaks
    hist = _git("log", "--all", "-G", r"AIza[0-9A-Za-z_-]{35}", "--oneline").stdout.strip()
    assert hist == "", hist


def test_tool_registry_has_no_dangerous_capabilities():
    """Hiçbir tool shell/dosya silme/SQL almaz: arg şemalarında 'command', 'sql', 'path' tipi serbest alan yok."""
    for name, spec in TOOLS.items():
        props = set(spec.args_model.model_fields)
        assert not (props & {"command", "cmd", "sql", "query", "path", "file_path", "url", "code", "script"}), \
            (name, props)
    # Tüm arg modelleri bilinmeyen alanı reddeder
    for spec in TOOLS.values():
        assert spec.args_model.model_config.get("extra") == "forbid"


def test_artifact_paths_stay_inside_artifact_dir(db, tmp_path):
    ex = ToolExecutor(ToolContext(project_id="demo-project", artifact_dir=tmp_path / "art"))
    tid = ex.start_task("x", "demo-project")
    r = ex.execute(tid, "create_spaghetti_chart", {"title": "../../evil"})
    assert r.status == ToolStatus.OK
    for a in r.artifacts:
        assert Path(a).resolve().is_relative_to((tmp_path / "art").resolve())
