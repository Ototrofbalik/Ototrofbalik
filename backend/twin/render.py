"""[Frontend] Digital Twin -> tek dosya HTML oynatıcı. Harici kaynak yok; kullanıcı metni yalnız JSON olarak gömülür."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from backend.twin.schema import TwinData

ASSET = Path(__file__).resolve().parent / "assets" / "player.html"
MAX_HTML_BYTES = 15 * 1024 * 1024


def _embed_json(data: TwinData) -> str:
    """JSON'u <script type=application/json> içine güvenle gömer (</script>, <!--, U+2028/2029 kaçışlı)."""
    s = json.dumps(data.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    return (s.replace("</", "<\\/").replace("<!--", "<\\u0021--")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def render_twin_html(data: TwinData, title: str) -> str:
    """Tek dosya, bağımsız (CDN/fetch yok) oynatıcı HTML'i üretir."""
    template = ASSET.read_text(encoding="utf-8")
    payload = {"__TWIN_TITLE__": html.escape(title, quote=True), "__TWIN_JSON__": _embed_json(data)}
    out = re.sub(r"__TWIN_TITLE__|__TWIN_JSON__", lambda m: payload[m.group(0)], template)   # tek geçiş
    if len(out.encode("utf-8")) > MAX_HTML_BYTES:
        raise ValueError("Digital Twin HTML 15 MB sınırını aşıyor; window_s ile pencereyi daraltın.")
    return out
