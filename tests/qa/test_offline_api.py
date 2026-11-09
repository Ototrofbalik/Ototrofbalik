"""[QA] Kontrol 8 — anahtarsız offline çalışma + FastAPI uçları (501 olmamalı) ve HTTP hata eşlemesi."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.database import configure_database
from backend.schemas import GeminiStatus, SimResult


@pytest.fixture
def client(tmp_path):
    configure_database(f"sqlite:///{tmp_path / 'api.db'}")
    from backend.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_no_key_in_environment_and_settings_inactive():
    s = Settings()
    assert s.gemini_api_key is None and s.gemini_active is False and s.external_data_consent is False


def test_health_and_integrations_work_without_key(client):
    assert client.get("/health").json() == {"status": "ok"}
    j = client.get("/integrations").json()
    assert j["google_sheets"]["connected"] is False and j["google_drive"]["connected"] is False


def test_api_demo_simulation_optimization_not_501_and_real(client, analysis):
    r = client.post("/projects/demo")
    assert r.status_code == 200 and r.json()["id"] == "demo-project" and r.json()["is_demo"] is True
    # --- simülasyon: 201, gerçek sayılar (bağımsız pipeline ile eşleşir)
    r = client.post("/projects/demo-project/simulations")
    assert r.status_code == 201, r.text
    res = SimResult.model_validate(r.json()["result"])
    assert res.kpis == analysis.baseline_sim.kpis and res.seed == 42
    run_id = r.json()["run_id"]
    assert client.get("/projects/demo-project/simulations").json()[0]["id"] == run_id
    assert client.get("/projects/demo-project/simulations/latest").status_code == 200
    assert client.get(f"/simulations/{run_id}").json()["kpis"]["completed"] == res.kpis.completed
    # --- optimizasyon: 201 (hızlı yapılandırma)
    r = client.post("/projects/demo-project/optimizations",
                    json={"iterations": 60, "simulate_alternatives": False, "n_alternatives": 3})
    assert r.status_code == 201, r.text
    opt = r.json()["result"]
    assert len(opt["alternatives"]) >= 3 and opt["best_alternative_id"]
    assert all(a["validation"]["valid"] for a in opt["alternatives"])
    oid = r.json()["optimization_id"]
    assert client.get("/projects/demo-project/optimizations").json()[0]["id"] == oid
    assert client.get("/projects/demo-project/optimizations/latest").status_code == 200
    assert client.get(f"/optimizations/{oid}").status_code == 200
    # --- uygulama: yeni sürüm, eski korunur
    best = opt["best_alternative_id"]
    r = client.post(f"/projects/demo-project/optimizations/{oid}/apply/{best}")
    assert r.status_code == 200
    assert len(client.get("/projects/demo-project/layout/versions").json()) == 2
    assert client.post("/projects/demo-project/layout/validate").json()["valid"] is True


def test_no_endpoint_returns_501(client):
    client.post("/projects/demo")
    paths = ["/health", "/integrations", "/projects", "/projects/demo-project", "/projects/demo-project/layout",
             "/projects/demo-project/layout/versions", "/projects/demo-project/simulations",
             "/projects/demo-project/optimizations", "/projects/demo-project/reports",
             "/projects/demo-project/export/json", "/projects/demo-project/export/excel",
             "/projects/demo-project/export/workstations.csv"]
    for p in paths:
        assert client.get(p).status_code == 200, p
    for p in ("/projects/demo-project/simulations", "/projects/demo-project/layout/validate"):
        assert client.post(p).status_code in (200, 201), p
    # route tablosu: ModuleNotReady -> 501 eşlemesi tetiklenmemeli (modüller yazıldı)
    import backend.services as sv

    assert sv.ModuleNotReady.__name__ == "ModuleNotReady"


def test_openapi_lists_all_documented_endpoints(client):
    paths = set(client.get("/openapi.json").json()["paths"])
    for need in ("/health", "/projects/demo", "/projects/{project_id}/simulations",
                 "/projects/{project_id}/optimizations", "/projects/{project_id}/layout/validate"):
        assert need in paths


def test_json_and_excel_roundtrip_via_api(client):
    client.post("/projects/demo")
    js = client.get("/projects/demo-project/export/json").text
    r = client.post("/projects/import/json", content=js)
    assert r.status_code == 201
    xl = client.get("/projects/demo-project/export/excel").content
    assert xl[:2] == b"PK"
    assert client.post("/projects/import/excel", content=xl).status_code == 201


# ----------------------------------------------------------------------------------------- hata eşlemesi
def test_unknown_resources_are_404_not_500(client):
    for m, p in (("get", "/projects/yok"), ("post", "/projects/yok/simulations"), ("post", "/projects/yok/optimizations"),
                 ("get", "/simulations/99999"), ("get", "/optimizations/99999")):
        assert getattr(client, m)(p).status_code == 404, p


def test_bad_input_is_422(client):
    client.post("/projects/demo")
    assert client.post("/projects/demo-project/simulations", json={"duration_s": -1, "resources": []}).status_code == 422
    assert client.post("/projects/demo-project/optimizations", json={"iterations": 0}).status_code == 422
    assert client.post("/projects/import/json", content="{bozuk").status_code == 422


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F-03: domain hataları (SimulationError/KeyError/UnreachableFlowError) HTTP 500 dönüyor")
def test_domain_errors_map_to_4xx_not_500(client):
    client.post("/projects/demo")
    lay = client.get("/projects/demo-project/layout").json()
    lay["id"] = "no-cut"
    lay["workstations"] = [w for w in lay["workstations"] if w["id"] != "cut"]    # ürün rotasındaki istasyon silindi
    assert client.put("/projects/demo-project/layout", json=lay).status_code == 200
    assert client.post("/projects/demo-project/simulations").status_code < 500
    assert client.post("/projects/demo-project/optimizations").status_code < 500


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F-03: kaynaksız (resources=[]) SimConfig -> SimulationError -> HTTP 500")
def test_zero_resource_simulation_is_client_error(client):
    client.post("/projects/demo")
    r = client.post("/projects/demo-project/simulations", json={"resources": [], "seed": 1})
    assert 400 <= r.status_code < 500


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F-03: erişilemeyen rota (çakışan layout kaydedildi) simülasyonda HTTP 500")
def test_unreachable_layout_simulation_is_client_error(client):
    client.post("/projects/demo")
    lay = client.get("/projects/demo-project/layout").json()
    lay["id"] = "ovl"
    lay["workstations"] = [dict(w, x=1, y=2) for w in lay["workstations"]]
    assert client.put("/projects/demo-project/layout", json=lay).status_code == 200
    assert 400 <= client.post("/projects/demo-project/simulations").status_code < 500


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F-04: proje id'si yol ayracı/'..' içerebiliyor (UI rapor klasörü REPORT_DIR/project_id)")
def test_project_id_with_path_traversal_rejected(client):
    from backend.demo.demo_factory import build_demo_project

    body = build_demo_project().model_dump(mode="json")
    body["id"] = "../../etc/x"
    assert client.post("/projects", json=body).status_code == 422


# ----------------------------------------------------------------------------------------- servis/agent offline
def test_services_work_offline_end_to_end(db):
    from backend import services

    rid, sim = services.run_simulation_for("demo-project")
    assert sim.kpis.completed == 62 and services.get_simulation(rid).kpis == sim.kpis
    oid, opt = services.run_optimization_for("demo-project", __import__("backend.schemas", fromlist=["x"]).OptimizationConfig(
        iterations=60, simulate_alternatives=False))
    assert len(opt.alternatives) >= 3
    assert services.validate_layout_for("demo-project").valid


def test_agent_status_without_key_is_missing_not_crash():
    from backend.agent.gemini_client import GeminiClient

    c = GeminiClient(Settings())
    assert c.status == GeminiStatus.API_KEY_MISSING
    with pytest.raises(Exception) as ei:
        c.generate([], system_prompt="", tools=[])
    assert "anahtar" in str(ei.value).lower()
