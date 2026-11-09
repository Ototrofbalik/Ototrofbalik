"""FastAPI uçları (TestClient)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.database import configure_database
from backend.demo.demo_factory import build_demo_project


@pytest.fixture
def client(tmp_path):
    configure_database(f"sqlite:///{tmp_path / 'api.db'}")
    from backend.main import app

    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_integrations_not_connected(client):
    j = client.get("/integrations").json()
    assert j["google_sheets"]["connected"] is False and j["google_drive"]["connected"] is False


def test_projects_demo_and_get(client):
    assert client.get("/projects").json() == []
    demo = client.post("/projects/demo").json()
    assert demo["is_demo"] is True
    assert client.get("/projects").json()[0]["id"] == demo["id"]
    got = client.get(f"/projects/{demo['id']}").json()
    assert got["layout"]["id"] == build_demo_project().layout.id
    assert client.get("/projects/yok").status_code == 404


def test_create_project_and_validation_error(client):
    body = build_demo_project().model_dump(mode="json")
    body["id"] = "p2"
    r = client.post("/projects", json=body)
    assert r.status_code == 201 and r.json()["id"] == "p2"
    body["unknown"] = 1
    assert client.post("/projects", json=body).status_code == 422


def test_layout_put_and_versions(client):
    demo = client.post("/projects/demo").json()
    pid = demo["id"]
    layout = client.get(f"/projects/{pid}/layout").json()
    layout["workstations"][0]["x"] += 1
    saved = client.put(f"/projects/{pid}/layout", json=layout)
    assert saved.status_code == 200 and saved.json()["version"] == 2
    versions = client.get(f"/projects/{pid}/layout/versions").json()
    assert len(versions) == 2 and versions[-1]["active"] is True
    old = client.get(f"/projects/{pid}/layout/versions/{versions[0]['id']}").json()
    assert old["id"] == demo["layout"]["id"]
    act = client.post(f"/projects/{pid}/layout/versions/{versions[0]['id']}/activate")
    assert act.status_code == 200
    assert client.get(f"/projects/{pid}/layout").json()["id"] == demo["layout"]["id"]
    assert client.get(f"/projects/{pid}/layout/versions/yok").status_code == 404


def test_compute_endpoints_501_or_ok(client):
    pid = client.post("/projects/demo").json()["id"]
    for method, url in (("post", f"/projects/{pid}/layout/validate"),
                        ("post", f"/projects/{pid}/simulations"),
                        ("post", f"/projects/{pid}/optimizations")):
        r = getattr(client, method)(url)
        assert r.status_code in (200, 201, 501), (url, r.status_code, r.text)
        if r.status_code == 501:
            assert "uygulanmadı" in r.json()["detail"]


def test_latest_sim_and_opt_404_when_absent(client):
    pid = client.post("/projects/demo").json()["id"]
    assert client.get(f"/projects/{pid}/simulations/latest").status_code == 404
    assert client.get(f"/projects/{pid}/optimizations/latest").status_code == 404
    assert client.get(f"/projects/{pid}/simulations").json() == []
    assert client.get("/projects/yok/simulations").status_code == 404


def test_export_import_endpoints(client):
    pid = client.post("/projects/demo").json()["id"]
    j = client.get(f"/projects/{pid}/export/json")
    assert j.status_code == 200
    imp = client.post("/projects/import/json", content=j.content)
    assert imp.status_code == 201 and imp.json()["id"] != pid
    assert client.post("/projects/import/json", content=b"{x").status_code == 422
    csv = client.get(f"/projects/{pid}/export/workstations.csv")
    assert csv.status_code == 200 and csv.text.startswith("id,name,kind")
    pytest.importorskip("openpyxl")
    x = client.get(f"/projects/{pid}/export/excel")
    assert x.status_code == 200
    imp2 = client.post("/projects/import/excel", content=x.content)
    assert imp2.status_code == 201
    assert client.post("/projects/import/excel", content=b"junk").status_code == 422


def test_reports_listing(client):
    from backend import services

    pid = client.post("/projects/demo").json()["id"]
    services.save_report(pid, "html", "x.html", "t")
    assert client.get(f"/projects/{pid}/reports").json()[0]["kind"] == "html"
