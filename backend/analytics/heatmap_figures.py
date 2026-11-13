"""Isı haritası Plotly figürleri (imzalar sözleşmeyle dondurulmuştur).

Tüm hücre değerleri girdideki TimeGrid / FromToMatrix nesnelerinden gelir; hiçbir sayı üretilmez.
Görsel dil: beyaz zemin, sıralı tek tonlu renk skalaları (renk körü dostu), Türkçe etiketler.
"""
from __future__ import annotations

import textwrap

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from backend.analytics.heatmaps import FromToMatrix, TimeGrid

_UTIL_SCALE = "Blues"
_QUEUE_SCALE = "Oranges"
_FT_SCALE = "Purples"
_WARM_FILL = "rgba(235,235,235,0.55)"
_WARN = "#b71c1c"
_FONT = {"family": "Arial, Helvetica, sans-serif", "size": 12, "color": "#212121"}
_METRIC_LABEL = {"utilization": "Kullanım oranı", "avg_queue": "Ortalama kuyruk", "max_queue": "En büyük kuyruk"}
_FT_LABEL = {
    "moves": ("hareket/sa", "Hareket sıklığı (hareket/sa)"),
    "metres": ("m/sa", "Taşıma yükü (m/sa)"),
    "distance": ("m", "İstasyonlar arası mesafe (m)"),
}
_MAX_CELL_TEXT_CELLS = 400
_WRAP = 170


# --------------------------------------------------------------------------- yardımcılar
def _clock(sec: float) -> str:
    s = int(round(sec))
    h, rem = divmod(s, 3600)
    m, ss = divmod(rem, 60)
    return f"{h:02d}:{m:02d}" + (f":{ss:02d}" if ss else "")


def _bin_labels(edges: list[float]) -> list[str]:
    return [f"{_clock(a)}–{_clock(b)}" for a, b in zip(edges[:-1], edges[1:], strict=False)]


def _is_util(grid: TimeGrid) -> bool:
    return grid.metric == "utilization"


def _fmt_cell(grid: TimeGrid, v: float | None) -> str:
    if v is None:
        return ""
    if _is_util(grid):
        return f"%{round(v * 100)}"
    if grid.metric == "max_queue":
        return f"{round(v)}"
    return f"{v:.1f}"


def _fmt_hover(grid: TimeGrid, v: float | None) -> str:
    if v is None:
        return "veri yok"
    if _is_util(grid):
        return f"%{v * 100:.1f} ({grid.unit})"
    if grid.metric == "max_queue":
        return f"{round(v)} {grid.unit}"
    return f"{v:.2f} {grid.unit}"


def _vmax(grids: list[TimeGrid]) -> float:
    vals = [v for g in grids for row in g.values for v in row if v is not None]
    m = max(vals) if vals else 0.0
    return m if m > 0 else 1.0


def _warm_bins(grid: TimeGrid) -> list[int]:
    if grid.warmup_s <= 0:
        return []
    return [j for j, e in enumerate(grid.bin_edges_s[1:]) if e <= grid.warmup_s + 1e-9]


def _grid_trace(grid: TimeGrid, rows: list[tuple[str, str]], zmin: float, zmax: float,
                showscale: bool, cbar_title: str) -> go.Heatmap:
    """rows = [(station_id, name)] gösterim sırası; gridde olmayan satırlar boş (None)."""
    labels = _bin_labels(grid.bin_edges_s)
    n_bins = len(labels)
    by_id = {sid: i for i, sid in enumerate(grid.station_ids)}
    z, text, custom = [], [], []
    for sid, name in rows:
        i = by_id.get(sid)
        vals: list[float | None] = list(grid.values[i]) if i is not None else [None] * n_bins
        vals = (vals + [None] * n_bins)[:n_bins]
        z.append(vals)
        text.append([_fmt_cell(grid, v) for v in vals])
        custom.append([[name, labels[j], _fmt_hover(grid, vals[j])] for j in range(n_bins)])
    show_text = len(rows) * n_bins <= _MAX_CELL_TEXT_CELLS
    metric = _METRIC_LABEL.get(grid.metric, grid.metric)
    return go.Heatmap(
        z=z, x=list(range(n_bins)), y=list(range(len(rows))),
        zmin=zmin, zmax=zmax, colorscale=_UTIL_SCALE if _is_util(grid) else _QUEUE_SCALE,
        xgap=2, ygap=2, hoverongaps=False,
        text=text if show_text else None, texttemplate="%{text}" if show_text else None,
        textfont={"size": 12}, customdata=custom,
        hovertemplate="<b>%{customdata[0]}</b><br>Aralık: %{customdata[1]}<br>"
                      f"{metric}: %{{customdata[2]}}<extra></extra>",
        showscale=showscale,
        colorbar={"title": {"text": cbar_title, "side": "right"}, "thickness": 14, "len": 0.9,
                  **({"tickformat": ".0%"} if _is_util(grid) else {})},
    )


def _wrap_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    for ln in lines:
        out.extend(textwrap.wrap(ln, _WRAP) or [""])
    return out


def _empty_figure(title: str, msg: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=msg, x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False,
                       font={"size": 14, "color": "#616161"})
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    fig.update_layout(title={"text": title, "x": 0.01}, plot_bgcolor="white", paper_bgcolor="white",
                      font=_FONT, margin={"l": 40, "r": 40, "t": 70, "b": 40})
    return fig


def _style_axes(fig: go.Figure, rows: list[tuple[str, str]], grid: TimeGrid, col: int | None) -> None:
    kw = {"row": 1, "col": col} if col else {}
    labels = _bin_labels(grid.bin_edges_s)
    n = len(labels)
    step = max(1, -(-n // 12))  # çok dilimde etiketleri seyrelt
    fig.update_xaxes(tickmode="array", tickvals=list(range(0, n, step)),
                     ticktext=[labels[j] for j in range(0, n, step)], range=[-0.5, n - 0.5],
                     title_text="Zaman aralığı (saat:dakika)", tickangle=-35 if n > 6 else 0,
                     showgrid=False, zeroline=False, **kw)
    fig.update_yaxes(tickmode="array", tickvals=list(range(len(rows))), ticktext=[r[1] for r in rows],
                     range=[len(rows) - 0.5, -0.5], title_text="İstasyon" if col in (None, 1) else None,
                     showgrid=False, zeroline=False, **kw)


def _shade_warmup(fig: go.Figure, grid: TimeGrid, n_rows: int, col: int | None) -> bool:
    warm = _warm_bins(grid)
    if not warm:
        return False
    xref = "x" if not col or col == 1 else f"x{col}"
    yref = "y" if not col or col == 1 else f"y{col}"
    for j in warm:
        fig.add_shape(type="rect", xref=xref, yref=yref, x0=j - 0.5, x1=j + 0.5, y0=-0.5, y1=n_rows - 0.5,
                      fillcolor=_WARM_FILL, line={"color": "#9e9e9e", "width": 1, "dash": "dot"}, layer="above")
    fig.add_annotation(xref=xref, yref="paper", x=(warm[0] + warm[-1]) / 2, y=1.0, yanchor="bottom",
                       text="ısınma", showarrow=False, font={"size": 10, "color": "#616161"})
    return True


def _footer(fig: go.Figure, lines: list[str], bottom: int) -> None:
    fig.update_layout(margin={"l": 90, "r": 40, "t": 90, "b": bottom})
    if lines:
        fig.add_annotation(text="<br>".join(_wrap_lines(lines)), xref="paper", yref="paper", x=0, y=0,
                           yshift=-(bottom - 60), xanchor="left", yanchor="top", showarrow=False, align="left",
                           font={"size": 10, "color": "#616161"})


def _footer_lines(grids: list[TimeGrid], warm: bool) -> list[str]:
    lines: list[str] = []
    if warm:
        lines.append("Soluk noktalı sütunlar ısınma dilimidir (istatistiklere dahil edilmeyen dönem).")
    seen: set[str] = set()
    for g in grids:
        for n in g.notes:
            if n not in seen:
                seen.add(n)
                lines.append(n)
    return lines


def _bottom_for(lines: list[str], base: int, n_bins: int) -> int:
    return base + 14 * len(_wrap_lines(lines)) + (40 if n_bins > 6 else 0)


# --------------------------------------------------------------------------- tek grid
def time_grid_figure(grid: TimeGrid, title: str = "", zmax: float | None = None) -> go.Figure:
    """İstasyon × zaman ısı haritası. zmax verilirse renk ölçeği sabitlenir (önce/sonra karşılaştırma)."""
    metric = _METRIC_LABEL.get(grid.metric, grid.metric)
    base_title = title or f"{metric} — istasyon × zaman"
    n_bins = len(grid.bin_edges_s) - 1
    if not grid.station_ids or n_bins < 1:
        return _empty_figure(base_title, "Gösterilecek veri yok (istasyon veya zaman dilimi bulunamadı).")
    rows = list(zip(grid.station_ids, grid.station_names, strict=True))
    if _is_util(grid):
        zmin, zhi = 0.0, 1.0 if zmax is None else zmax
    else:
        zmin, zhi = 0.0, zmax if zmax is not None else _vmax([grid])
    fig = go.Figure(_grid_trace(grid, rows, zmin, zhi, True, f"{metric} ({grid.unit})"))
    _style_axes(fig, rows, grid, None)
    warm = _shade_warmup(fig, grid, len(rows), None)
    t = base_title
    if grid.truncated:
        t += f"<br><span style='font-size:12px;color:{_WARN}'>UYARI: olay logu kesildi; son dilimler eksik olabilir.</span>"
    fig.update_layout(title={"text": t, "x": 0.01}, plot_bgcolor="white", paper_bgcolor="white", font=_FONT)
    lines = _footer_lines([grid], warm)
    if grid.truncated:
        lines.append("Olay logu kesildiği için son dilimler eksik olabilir.")
    lines.append(f"Gösterilen replikasyon: {grid.replication_shown}.")
    bottom = _bottom_for(lines, 90, n_bins)
    _footer(fig, lines, bottom)
    fig.update_layout(height=max(380, 50 * len(rows) + bottom + 90))
    return fig


# --------------------------------------------------------------------------- karşılaştırma
def time_grid_compare_figure(before: TimeGrid, after: TimeGrid, labels: tuple[str, str] = ("Önce", "Sonra")) -> go.Figure:
    """İki TimeGrid yan yana, ORTAK renk ölçeği."""
    metric = _METRIC_LABEL.get(before.metric, before.metric)
    title = f"{metric} — {labels[0]} / {labels[1]}"
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for g in (before, after):
        for sid, name in zip(g.station_ids, g.station_names, strict=True):
            if sid not in seen:
                seen.add(sid)
                rows.append((sid, name))
    if not rows or min(len(before.bin_edges_s), len(after.bin_edges_s)) < 2:
        return _empty_figure(title, "Karşılaştırılacak veri yok.")
    if _is_util(before) and _is_util(after):
        zmin, zmax = 0.0, 1.0
    else:
        zmin, zmax = 0.0, _vmax([before, after])
    fig = make_subplots(rows=1, cols=2, subplot_titles=labels, horizontal_spacing=0.06, shared_yaxes=True)
    fig.add_trace(_grid_trace(before, rows, zmin, zmax, False, ""), row=1, col=1)
    fig.add_trace(_grid_trace(after, rows, zmin, zmax, True, f"{metric} ({after.unit})"), row=1, col=2)
    _style_axes(fig, rows, before, 1)
    _style_axes(fig, rows, after, 2)
    fig.update_yaxes(title_text=None, showticklabels=False, row=1, col=2)
    warm = _shade_warmup(fig, before, len(rows), 1)
    warm = _shade_warmup(fig, after, len(rows), 2) or warm
    if before.truncated or after.truncated:
        who = [lb for lb, g in zip(labels, (before, after), strict=True) if g.truncated]
        title += (f"<br><span style='font-size:12px;color:{_WARN}'>UYARI: olay logu kesildi "
                  f"({', '.join(who)}); son dilimler eksik olabilir.</span>")
    fig.update_layout(title={"text": title, "x": 0.01}, plot_bgcolor="white", paper_bgcolor="white", font=_FONT)
    lines = [f"Her iki panel ortak renk ölçeğini kullanır ({zmin:g}–{zmax:g})."]
    lines += _footer_lines([before, after], warm)
    bottom = _bottom_for(lines, 90, max(len(before.bin_edges_s), len(after.bin_edges_s)) - 1)
    _footer(fig, lines, bottom)
    fig.update_layout(height=max(400, 50 * len(rows) + bottom + 90))
    return fig


# --------------------------------------------------------------------------- from-to
def _ft_fmt(v: float) -> str:
    return f"{v:.0f}" if abs(v) >= 100 else f"{v:.1f}".rstrip("0").rstrip(".")


def from_to_figure(m: FromToMatrix, value: str = "moves", title: str = "") -> go.Figure:
    """value: 'moves' (hareket/sa) | 'metres' (m/sa) | 'distance' (m). Boş hücreler boş kalır."""
    if value not in _FT_LABEL:
        raise ValueError(f"Geçersiz value={value!r}; 'moves', 'metres' veya 'distance' olmalı.")
    unit, label = _FT_LABEL[value]
    base = title or f"From-To matrisi — {label}"
    names = list(m.station_names)
    n = len(names)
    if n == 0:
        return _empty_figure(base, "Gösterilecek veri yok (istasyon bulunamadı).")
    src = {"moves": m.moves_per_hour, "metres": m.metres_per_hour, "distance": m.distance_m}[value]
    z: list[list[float | None]] = []
    text: list[list[str]] = []
    for i in range(n):
        row: list[float | None] = []
        trow: list[str] = []
        for j in range(n):
            v = src[i][j] if i < len(src) and j < len(src[i]) else None
            if v is None or v == 0:
                row.append(None)
                trow.append("")
            else:
                row.append(v)
                trow.append(_ft_fmt(v))
        z.append(row)
        text.append(trow)
    custom = [[[names[i], names[j]] for j in range(n)] for i in range(n)]
    fig = go.Figure(go.Heatmap(
        z=z, x=list(range(n)), y=list(range(n)), colorscale=_FT_SCALE, xgap=2, ygap=2, hoverongaps=False,
        zmin=0, zmax=max([v for r in z for v in r if v is not None] or [1.0]), text=text, texttemplate="%{text}", textfont={"size": 12}, customdata=custom,
        hovertemplate="Kaynak: <b>%{customdata[0]}</b><br>Hedef: <b>%{customdata[1]}</b><br>"
                      f"{label.split(' (')[0]}: %{{z:.2f}} {unit}<extra></extra>",
        colorbar={"title": {"text": unit, "side": "right"}, "thickness": 14, "len": 0.9}))
    for i in range(n):  # köşegen soluk
        fig.add_shape(type="rect", x0=i - 0.5, x1=i + 0.5, y0=i - 0.5, y1=i + 0.5,
                      fillcolor="rgba(224,224,224,0.85)", line={"color": "#bdbdbd", "width": 1}, layer="above")
    fig.update_xaxes(title_text="Hedef", tickmode="array", tickvals=list(range(n)), ticktext=names,
                     range=[-0.5, n - 0.5], tickangle=-35 if n > 6 else 0, showgrid=False, zeroline=False)
    fig.update_yaxes(title_text="Kaynak", tickmode="array", tickvals=list(range(n)), ticktext=names,
                     range=[n - 0.5, -0.5], showgrid=False, zeroline=False)
    t = base
    if m.mode != "network":
        t += (f"<br><span style='font-size:12px;color:{_WARN}'>UYARI: mesafe modu '{m.mode}'; "
              "değerler yaklaşık mesafedir (koridor ağı yolu değil).</span>")
    fig.update_layout(title={"text": t, "x": 0.01}, plot_bgcolor="white", paper_bgcolor="white", font=_FONT)
    lines = [f"Toplam taşıma yükü: {m.total_metres_per_hour:.1f} m/sa (mod: {m.mode}). "
             "Boş hücre = o yönde hareket yok; soluk köşegen = aynı istasyon."]
    lines += list(m.notes)
    bottom = _bottom_for(lines, 90, n)
    _footer(fig, lines, bottom)
    fig.update_layout(height=max(420, 50 * n + bottom + 90))
    return fig
