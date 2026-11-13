"""Gerçek demo simülasyonu ile oynatıcı <-> veri sözleşmesi uyumu (fixture'ın yakalayamadığı hatalar için).

Regresyon: LOAD/UNLOAD segmentlerinde `ref` bir *yol* indeksidir (istasyon değil). Fixture'da istasyon ve yol
sayısı aynı olduğundan bu hata yalnız gerçek veride görünüyordu (araçlar yanlış istasyonda çizilirdi).
"""
from __future__ import annotations

import json
import subprocess

import pytest

from backend.demo.demo_factory import build_demo_project
from backend.services import compute_flow
from backend.simulation.engine import run_simulation
from backend.twin import build_twin_data
from backend.twin.render import render_twin_html
from backend.twin.schema import VSEG_LOAD, VSEG_TRAVEL, VSEG_UNLOAD
from tests.test_twin_browser import _node_ok  # noqa: F401  (yeniden kullanım)

JS = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch({ args: ['--no-sandbox'] });
  const pg = await b.newPage({ viewport: { width: 1400, height: 900 } });
  const errs = []; pg.on('pageerror', e => errs.push(e.message)); pg.on('console', m => { if (m.type()==='error') errs.push(m.text()); });
  await pg.goto('file://' + process.argv[2]); await pg.waitForTimeout(500);
  const times = JSON.parse(process.argv[3]); const out = [];
  await pg.evaluate(() => window.__twin.pause());
  for (const t of times) {
    await pg.evaluate(t => window.__twin.seek(t), t);
    out.push({ t, st: await pg.evaluate(() => window.__twin.getState()) });
  }
  console.log(JSON.stringify({ errs, out })); await b.close();
})();
"""


@pytest.fixture(scope="module")
def real():
    p = build_demo_project()
    r, _ = compute_flow(p)
    sim = run_simulation(p, r)
    return sim, build_twin_data(p.layout, r, sim, products=p.products), p


def test_vehicle_positions_match_data_in_browser(real, tmp_path):
    if not _node_ok():
        pytest.skip("node veya playwright yok")
    sim, d, p = real
    # her tür (LOAD/TRAVEL/UNLOAD) için bir örnek an ve beklenen konum (Python verisinden)
    picks: list[tuple[float, int, tuple[float, float]]] = []
    seen: set[int] = set()
    for i, k in enumerate(d.vsegs.kind):
        if k in (VSEG_LOAD, VSEG_TRAVEL, VSEG_UNLOAD) and k not in seen and d.vsegs.t1[i] - d.vsegs.t0[i] > 4:
            P = d.scene.paths[d.vsegs.ref[i]]
            t = round((d.vsegs.t0[i] + d.vsegs.t1[i]) / 2, 2) if k == VSEG_TRAVEL else round(d.vsegs.t0[i] + 1.0, 2)
            exp = (P.pts[0] if k == VSEG_LOAD else P.pts[-1] if k == VSEG_UNLOAD else None)
            if exp is not None:
                picks.append((t, d.vsegs.veh[i], (exp[0], exp[1])))
                seen.add(k)
    assert len(picks) == 2, "demoda hem LOAD hem UNLOAD segmenti bulunmalı"
    html = tmp_path / "t.html"
    html.write_text(render_twin_html(d, "Demo"), encoding="utf-8")
    js = tmp_path / "c.js"
    js.write_text(JS, encoding="utf-8")
    root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
    r = subprocess.run(["node", str(js), str(html), json.dumps([t for t, _, _ in picks])], capture_output=True,
                       text=True, timeout=120, env={**__import__("os").environ, "NODE_PATH": root})
    if r.returncode != 0 and "Executable doesn't exist" in r.stderr:
        pytest.skip("Chromium başlatılamadı")
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert res["errs"] == []
    for (t, vi, (ex, ey)), o in zip(picks, res["out"], strict=True):
        veh = next(v for v in o["st"]["vehicles"] if v["id"] == d.vehicles[vi].id)
        assert abs(veh["x"] - ex) < 0.05 and abs(veh["y"] - ey) < 0.05, (t, veh, (ex, ey))


def test_final_counters_match_sim_in_browser(real, tmp_path):
    if not _node_ok():
        pytest.skip("node veya playwright yok")
    sim, d, _ = real
    html = tmp_path / "t.html"
    html.write_text(render_twin_html(d, "Demo"), encoding="utf-8")
    js = tmp_path / "c.js"
    js.write_text(JS, encoding="utf-8")
    root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
    r = subprocess.run(["node", str(js), str(html), json.dumps([d.meta.t_end])], capture_output=True, text=True,
                       timeout=120, env={**__import__("os").environ, "NODE_PATH": root})
    if r.returncode != 0 and "Executable doesn't exist" in r.stderr:
        pytest.skip("Chromium başlatılamadı")
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert res["errs"] == []
    assert res["out"][0]["st"]["completed"] == sim.kpis.completed


def test_real_data_not_truncated_and_in_budget(real):
    _, d, _ = real
    assert d.meta.truncated is False
    assert len(render_twin_html(d, "x").encode()) < 400_000
