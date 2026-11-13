"""Digital Twin HTML üretimi: tek dosya, harici kaynak yok, XSS-güvenli gömme."""
from __future__ import annotations

import json
import re
from pathlib import Path

from backend.twin.render import render_twin_html
from backend.twin.schema import TwinData

FIXTURE = Path(__file__).parent / "fixtures" / "twin_mini.json"
EVIL = "</script><img src=x onerror=alert(1)>"


def _data() -> TwinData:
    return TwinData.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def test_single_document_no_external_resources():
    html = render_twin_html(_data(), "Başlık")
    assert html.lower().count("<html") == 1
    for bad in ("<script src", "<link", "http://", "https://", "@import", "fetch(", "XMLHttpRequest", "innerHTML"):
        assert bad not in html, bad
    assert "window.__twin" in html


def test_embedded_json_roundtrips_to_schema():
    d = _data()
    html = render_twin_html(d, "t")
    m = re.search(r'<script type="application/json" id="twin-data">(.*?)</script>', html, re.S)
    assert m
    assert TwinData.model_validate(json.loads(m.group(1))) == d


def test_xss_station_name_and_title_are_neutralised():
    d = _data()
    d.scene.stations[0].name = EVIL
    d.meta.notes.append("Türkçe: ığüşöç   satır")
    html = render_twin_html(d, EVIL)
    outside = re.sub(r'id="twin-data">.*?</script>', "", html, flags=re.S)
    assert "<img" not in outside   # JSON içinde yalnız metin olarak durur
    assert html.count("</script>") == html.count("<script")   # ham kapanış yalnız gerçek bloklarda
    m = re.search(r'id="twin-data">(.*?)</script>', html, re.S)
    assert m
    data = json.loads(m.group(1))
    assert data["scene"]["stations"][0]["name"] == EVIL
    assert "ığüşöç" in m.group(1) and " " not in m.group(1)
    assert "&lt;/script&gt;" in html   # başlık kaçışlı


def test_placeholder_in_title_is_not_reexpanded():
    html = render_twin_html(_data(), "__TWIN_JSON__")
    assert html.count('"schema_version"') == 1
