"""Digital Twin oynatıcısı gerçek Chromium'da (node + playwright yoksa atlanır)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.twin.render import render_twin_html
from backend.twin.schema import TwinData

ROOT = Path(__file__).parent
FIXTURE = ROOT / "fixtures" / "twin_mini.json"
SCRIPT = ROOT / "manual" / "twin_browser_check.mjs"


def _node_ok() -> bool:
    if not shutil.which("node") or not shutil.which("npm"):
        return False
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=30).stdout.strip()
        r = subprocess.run(["node", "-e", f"require(require('path').join({root!r},'playwright'))"],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _run(html: str, out: Path, w: int, h: int, times: str) -> dict:
    page = out / "page.html"
    page.write_text(html, encoding="utf-8")
    r = subprocess.run(["node", str(SCRIPT), str(page), str(out / "shots"), str(w), str(h), times],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        if "Executable doesn't exist" in r.stderr or "browserType.launch" in r.stderr:
            pytest.skip("Chromium başlatılamadı")
        raise AssertionError(r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def need_browser():
    if not _node_ok():
        pytest.skip("node veya playwright yok")


def _fx() -> TwinData:
    return TwinData.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def test_player_runs_without_errors(need_browser, tmp_path):
    d = _fx()
    res = _run(render_twin_html(d, "Fixture"), tmp_path, 1600, 1000, f"0,3600,{d.meta.t_end}")
    assert res["errors"] == []
    assert res["colors"] >= 3
    assert res["playAfter"] > res["playStart"]
    assert len(res["shots"]) == 3 and all(Path(s).stat().st_size > 5000 for s in res["shots"])
    comp = d.series["completed_units"][1][-1]
    assert float(res["panelCompleted"].replace(".", "").replace(",", ".")) == comp
    assert res["stateEnd"]["completed"] == comp


def test_truncated_log_locks_timeline(need_browser, tmp_path):
    d = _fx()
    d.meta.truncated = True
    d.meta.t_valid_end = 400.0
    res = _run(render_twin_html(d, "Kesik"), tmp_path, 1200, 800, "400,10000")
    assert res["errors"] == []
    assert res["tMax"] == 400.0
    assert res["stateEnd"]["t"] == 400.0


def test_narrow_screen_has_no_horizontal_scroll(need_browser, tmp_path):
    res = _run(render_twin_html(_fx(), "Dar"), tmp_path, 360, 800, "250")
    assert res["errors"] == []
    assert res["bodyOverflowX"] is False
