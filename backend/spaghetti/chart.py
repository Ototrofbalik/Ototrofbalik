"""[Layout&Routing Engineer] Spaghetti Chart (Plotly).

Renk = araç tipi, çizgi kalınlığı = frekans (hareket/saat, doğrusal min-max), ok = yön, rozet = hareket sırası.
Ortak koridordaki rotalar yalnız GÖRSEL olarak paralel şeritlere ofsetlenir (Route/FlowMetrics nesnelerine yazılmaz;
hover ve metrikler gerçek mesafeyi kullanır). Koridorlar metre cinsinden dikdörtgen dolgu olarak çizilir.
Not: Route frekans taşımaz; frekans FlowMetrics.cells (from,to) hücresinden okunur. Aynı (from,to) çiftinde
birden fazla araç varsa hücre toplamı her araç çizgisine yazılır (hover'da belirtilir).
"""
from __future__ import annotations

import math
from collections import Counter

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from backend.schemas import FlowMetrics, Layout, Route, RouteSet, ValidationIssue, VehicleType

VEHICLE_COLOR = {
    VehicleType.OPERATOR: "#1f77b4",
    VehicleType.FORKLIFT: "#ff7f0e",
    VehicleType.AGV: "#2ca02c",
}
VEHICLE_LABEL = {
    VehicleType.OPERATOR: "Operatör",
    VehicleType.FORKLIFT: "Forklift",
    VehicleType.AGV: "AGV",
}
_RED = "#d62728"
_STATION_FILL = "rgba(120,144,156,0.35)"
_STATION_LINE = "#455a64"
_WIDTH_SAMPLE_COLOR = "#616161"   # VEHICLE_COLOR içinde OLMAMALI (sahte lejant izleri rota sayılmasın)

W_MIN, W_MAX, W_SINGLE = 2.0, 9.0, 5.0
MAX_BADGES = 30
MAX_TABLE_ROWS = 12
_BADGE_T = (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.15, 0.85)
_Pt = tuple[float, float]
_Box = tuple[float, float, float, float]


# --------------------------------------------------------------------------- yardımcılar
def _sp(row: int | None, col: int | None) -> dict:
    return {"row": row, "col": col} if row is not None else {}


def _key(a: _Pt, b: _Pt) -> tuple[_Pt, _Pt]:
    ra, rb = (round(a[0], 3), round(a[1], 3)), (round(b[0], 3), round(b[1], 3))
    return (ra, rb) if ra <= rb else (rb, ra)


def _pt_seg_dist(p: _Pt, a: _Pt, b: _Pt) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    ll = dx * dx + dy * dy
    t = 0.0 if ll < 1e-18 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / ll))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _seg_hits_box(a: _Pt, b: _Pt, box: _Box) -> bool:
    """Liang-Barsky: [a,b] doğru parçası kutuyla (x0,y0,x1,y1) kesişiyor mu?"""
    x0, y0, x1, y1 = box
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if abs(p) < 1e-12:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return False
            t0 = max(t0, t)
        else:
            if t < t0:
                return False
            t1 = min(t1, t)
    return True


def _in_box(p: _Pt, box: _Box, pad: float = 0.0) -> bool:
    return box[0] - pad <= p[0] <= box[2] + pad and box[1] - pad <= p[1] <= box[3] + pad


# --------------------------------------------------------------------------- paralel ofset (yalnız görsel)
def _split_paths(paths: list[list[_Pt]]) -> list[list[_Pt]]:
    """Tüm rotaların düğüm kümesiyle segmentleri böler (ortak ara noktalar; paylaşım yuvarlamayla kaçmasın)."""
    nodes = sorted({(round(p[0], 3), round(p[1], 3)) for path in paths for p in path})
    out: list[list[_Pt]] = []
    for path in paths:
        res: list[_Pt] = []
        for a, b in zip(path, path[1:], strict=False):
            if not res:
                res.append(a)
            dx, dy = b[0] - a[0], b[1] - a[1]
            ll = dx * dx + dy * dy
            if ll < 1e-12:
                continue
            xl, xh = min(a[0], b[0]) - 1e-6, max(a[0], b[0]) + 1e-6
            yl, yh = min(a[1], b[1]) - 1e-6, max(a[1], b[1]) + 1e-6
            mids = []
            for n in nodes:
                if not (xl <= n[0] <= xh and yl <= n[1] <= yh):
                    continue
                t = ((n[0] - a[0]) * dx + (n[1] - a[1]) * dy) / ll
                if 1e-6 < t < 1 - 1e-6 and _pt_seg_dist(n, a, b) < 1e-6:
                    mids.append((t, n))
            mids.sort()
            res.extend(n for _, n in mids)
            res.append(b)
        out.append(res if res else list(path))
    return out


def _segments(path: list[_Pt]):
    for a, b in zip(path, path[1:], strict=False):
        if math.dist(a, b) > 1e-9:
            yield a, b


def _offset_paths(routes_shown: list[Route], layout: Layout) -> dict[int, list[_Pt]]:
    """Rota başına görsel ofsetli polyline (yalnız network modu). Route nesnelerine DOKUNMAZ."""
    raw = {i: [(p.x, p.y) for p in r.points] for i, r in enumerate(routes_shown)}
    if not routes_shown or any(r.mode != "network" for r in routes_shown):
        return raw
    idxs = list(raw)
    split = dict(zip(idxs, _split_paths([raw[i] for i in idxs]), strict=True))
    seg_users: dict[tuple, list[int]] = {}
    for i in idxs:
        for a, b in _segments(split[i]):
            k = _key(a, b)
            if k[0] == k[1]:
                continue
            users = seg_users.setdefault(k, [])
            if i not in users:
                users.append(i)
    # --- şerit atama: rota başına tek, açgözlü renklendirme
    neigh: dict[int, set[int]] = {i: set() for i in idxs}
    for users in seg_users.values():
        for u in users:
            neigh[u].update(v for v in users if v != u)
    order = sorted(idxs, key=lambda i: (-len(raw[i]), routes_shown[i].vehicle.value,
                                       routes_shown[i].from_id, routes_shown[i].to_id))
    lane: dict[int, int] = {}
    for i in order:
        used = {lane[j] for j in neigh[i] if j in lane}
        lane[i] = next(n for n in range(len(idxs) + 1) if n not in used)
    # --- segment bağlamı (koridor / istasyon kolu) ve şerit kümeleri
    corr_segs = [(c.id, c.width, [(p.x, p.y) for p in c.points]) for c in layout.corridors]

    def ctx_of(k: tuple[_Pt, _Pt]) -> tuple[tuple, float]:
        best = None
        for cid, w, pts in corr_segs:
            for a, b in zip(pts, pts[1:], strict=False):
                if _pt_seg_dist(k[0], a, b) < 1e-3 and _pt_seg_dist(k[1], a, b) < 1e-3:
                    if best is None or w > best[1]:
                        best = (("c", cid), w)
        return best if best else (("a", k), 1.0)

    seg_ctx = {k: ctx_of(k) for k in seg_users}
    ctx_lanes: dict[tuple, set[int]] = {}
    for k, users in seg_users.items():
        ctx_lanes.setdefault(seg_ctx[k][0], set()).update(lane[u] for u in users)

    def seg_d(k: tuple[_Pt, _Pt], i: int) -> float:
        if len(seg_users[k]) <= 1:
            return 0.0
        ctx, w = seg_ctx[k]
        ls = sorted(ctx_lanes[ctx])
        n = len(ls)
        s = min(0.5, 0.8 * w / n)
        return (ls.index(lane[i]) - (n - 1) / 2) * s

    out: dict[int, list[_Pt]] = {}
    for i in idxs:
        segs = []   # (a, b, u, normal, d)
        for a, b in _segments(split[i]):
            k = _key(a, b)
            ln = math.dist(a, b)
            u = ((b[0] - a[0]) / ln, (b[1] - a[1]) / ln)
            cx, cy = k[1][0] - k[0][0], k[1][1] - k[0][1]
            cl = math.hypot(cx, cy)
            segs.append((a, b, u, (-cy / cl, cx / cl), seg_d(k, i)))   # normal: kanonik yönün solu
        if not segs:
            out[i] = raw[i]
            continue
        pts: list[_Pt] = [raw[i][0]]
        for s0, s1 in zip(segs, segs[1:], strict=False):
            _a0, p, u0, n0, d0 = s0
            _a1, _b1, u1, n1, d1 = s1
            q0 = (p[0] + n0[0] * d0, p[1] + n0[1] * d0)
            q1 = (p[0] + n1[0] * d1, p[1] + n1[1] * d1)
            cr = u0[0] * u1[1] - u0[1] * u1[0]
            if abs(cr) < 1e-9:      # paralel: aynı d ise tek nokta, değilse basamak
                pts.extend([q0] if abs(d0 - d1) < 1e-9 else [q0, q1])
                continue
            t = ((q1[0] - q0[0]) * u1[1] - (q1[1] - q0[1]) * u1[0]) / cr
            m = (q0[0] + t * u0[0], q0[1] + t * u0[1])
            if math.dist(m, p) > 3 * max(abs(d0), abs(d1), 1e-9):
                pts.extend([q0, q1])    # bevel (miter patlaması)
            else:
                pts.append(m)
        pts.append(raw[i][-1])
        out[i] = pts
    return out


def _cum(path: list[_Pt]) -> list[float]:
    c = [0.0]
    for a, b in zip(path, path[1:], strict=False):
        c.append(c[-1] + math.dist(a, b))
    return c


def _at(path: list[_Pt], cum: list[float], s: float) -> tuple[float, float, float]:
    """Yay uzunluğu s noktasında (x, y, ok açısı[derece, 'up' referansı, saat yönü])."""
    s = max(0.0, min(cum[-1], s))
    j = 0
    while j < len(path) - 2 and cum[j + 1] < s - 1e-12:
        j += 1
    a, b = path[j], path[j + 1]
    seg = cum[j + 1] - cum[j]
    t = 0.0 if seg < 1e-12 else (s - cum[j]) / seg
    dx, dy = b[0] - a[0], b[1] - a[1]
    return a[0] + dx * t, a[1] + dy * t, math.degrees(math.atan2(dx, dy))


# --------------------------------------------------------------------------- taban çizim
def _label_boxes(layout: Layout, paths: list[list[_Pt]] | None) -> dict[str, dict]:
    """İstasyon etiketi: istasyon DIŞINDA, üstte ya da altta (rota geçişi daha az olan kenar; eşitte üst)."""
    fac = layout.facility
    res: dict[str, dict] = {}
    for s in layout.workstations:
        r, c = s.footprint(), s.center()
        w, h = len(s.name) * 0.25, 0.8
        top = (c.x - w / 2, r.y2, c.x + w / 2, r.y2 + h)
        bot = (c.x - w / 2, r.y - h, c.x + w / 2, r.y)
        cnt = {"top": 0, "bot": 0}
        for path in paths or []:
            for a, b in zip(path, path[1:], strict=False):
                cnt["top"] += _seg_hits_box(a, b, top)
                cnt["bot"] += _seg_hits_box(a, b, bot)
        side = "top" if cnt["top"] <= cnt["bot"] else "bot"
        if side == "top" and r.y2 + 0.8 > fac.height:
            side = "bot"
        elif side == "bot" and r.y - 0.8 < 0:
            side = "top"
        if side == "top":
            res[s.id] = {"box": top, "pos": {"x": c.x, "y": r.y2, "yanchor": "bottom", "yshift": 3}}
        else:
            res[s.id] = {"box": bot, "pos": {"x": c.x, "y": r.y, "yanchor": "top", "yshift": -3}}
    return res


def _draw_base(fig: go.Figure, layout: Layout, row: int | None = None, col: int | None = None,
               bad_ids: set[str] | None = None, paths: list[list[_Pt]] | None = None) -> dict[str, dict]:
    bad_ids = bad_ids or set()
    sp = _sp(row, col)
    fac = layout.facility
    fig.add_shape(type="rect", x0=0, y0=0, x1=fac.width, y1=fac.height,
                  line={"color": "#37474f", "width": 2}, fillcolor="rgba(0,0,0,0)", layer="below", **sp)
    for c in layout.corridors:
        bad = c.id in bad_ids
        xs: list[float | None] = []
        ys: list[float | None] = []
        h = c.width / 2
        for a, b in zip(c.points, c.points[1:], strict=False):
            ln = math.hypot(b.x - a.x, b.y - a.y)
            if ln < 1e-9:
                continue
            ux, uy = (b.x - a.x) / ln, (b.y - a.y) / ln
            nx, ny = -uy, ux
            p0 = (a.x - ux * h, a.y - uy * h)
            p1 = (b.x + ux * h, b.y + uy * h)
            corners = [(p0[0] + nx * h, p0[1] + ny * h), (p1[0] + nx * h, p1[1] + ny * h),
                       (p1[0] - nx * h, p1[1] - ny * h), (p0[0] - nx * h, p0[1] - ny * h)]
            corners.append(corners[0])
            xs += [q[0] for q in corners] + [None]
            ys += [q[1] for q in corners] + [None]
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode="lines", fill="toself", line={"width": 0},
            fillcolor="rgba(214,39,40,0.30)" if bad else "rgba(158,158,158,0.25)", hoveron="fills",
            hoverinfo="text", text=f"Koridor {c.id} ({c.width:g} m{', tek yön' if c.one_way else ''})",
            showlegend=False), **sp)
    for o in layout.obstacles:
        r, bad = o.rect, o.id in bad_ids
        fig.add_shape(type="rect", x0=r.x, y0=r.y, x1=r.x2, y1=r.y2,
                      line={"color": _RED if bad else "#212121", "width": 2},
                      fillcolor="rgba(214,39,40,0.45)" if bad else "rgba(33,33,33,0.55)", **sp)
    for s in layout.workstations:
        r, bad = s.footprint(), s.id in bad_ids
        fig.add_shape(type="rect", x0=r.x, y0=r.y, x1=r.x2, y1=r.y2, layer="below",
                      line={"color": _RED if bad else _STATION_LINE, "width": 3 if bad else 1.5},
                      fillcolor="rgba(214,39,40,0.40)" if bad else _STATION_FILL, **sp)
    labels = _label_boxes(layout, paths)
    for s in layout.workstations:
        bad = s.id in bad_ids
        fig.add_annotation(
            text=s.name, showarrow=False, bgcolor="rgba(255,255,255,0.88)", bordercolor=_STATION_LINE,
            borderwidth=1, borderpad=2, font={"size": 11, "color": _RED if bad else "#212121"},
            name=f"station:{s.id}", **labels[s.id]["pos"], **sp)
    return labels


def _style_axes(fig: go.Figure, layout: Layout, row: int | None = None, col: int | None = None) -> None:
    fac = layout.facility
    sp = _sp(row, col)
    anchor = "x" if (row is None or (row, col) == (1, 1)) else f"x{(col or 1)}"
    fig.update_xaxes(range=[-1, fac.width + 1], title_text="x (m)", constrain="domain", **sp)
    fig.update_yaxes(range=[-1, fac.height + 1], title_text="y (m)", scaleanchor=anchor, scaleratio=1, **sp)


def _cell_moves(metrics: FlowMetrics) -> dict[tuple[str, str], float]:
    return {(c.from_id, c.to_id): c.moves_per_hour for c in metrics.cells}


def _shown(routes: RouteSet, vehicles: set[VehicleType] | None) -> list[tuple[int, Route]]:
    """(hareket no, rota): numara filtreden bağımsız, RouteSet içindeki sıraya göre 1'den başlar."""
    return [(i + 1, r) for i, r in enumerate(routes.routes)
            if r.reachable and len(r.points) >= 2 and (vehicles is None or r.vehicle in vehicles)]


def _freq_range(shown: list[tuple[int, Route]], moves: dict[tuple[str, str], float]) -> tuple[float, float] | None:
    fs = [moves.get((r.from_id, r.to_id), 0.0) for _, r in shown]
    return (min(fs), max(fs)) if fs else None


def _width(freq: float, fmin: float, fmax: float) -> float:
    """Doğrusal min-max kalınlık (px); frekanslar eşitse W_SINGLE, frekans 0 ise W_MIN."""
    if freq <= 0:
        return W_MIN
    if fmax - fmin < 1e-9:
        return W_SINGLE
    return W_MIN + (W_MAX - W_MIN) * (freq - fmin) / (fmax - fmin)


def _pick_badge(path: list[_Pt], cum: list[float], placed: list[_Pt], blocked: list[_Box]) -> float:
    total = cum[-1]
    for t in _BADGE_T:
        x, y, _ = _at(path, cum, t * total)
        if all(math.dist((x, y), q) >= 1.2 for q in placed) and not any(_in_box((x, y), b) for b in blocked):
            return t * total
    return 0.5 * total


def _arrow_positions(total: float, badge_s: float | None, path: list[_Pt], cum: list[float]) -> list[float]:
    if total < 8:
        pos = [0.33 * total, 0.66 * total]
    else:
        n = max(2, min(8, int(total / 4)))
        pos = [(k + 0.5) * total / n for k in range(n)]
    if badge_s is None:
        return pos
    bx, by, _ = _at(path, cum, badge_s)
    out = []
    for s in pos:
        x, y, _a = _at(path, cum, s)
        if math.dist((x, y), (bx, by)) < 1.0:
            cands = [min(max(s + dlt, 1.0 if total > 2 else 0.0), total - 1.0 if total > 2 else total)
                     for dlt in (1.2, -1.2)]
            s = max(cands, key=lambda c: math.dist(_at(path, cum, c)[:2], (bx, by)))
        out.append(s)
    return out


def _draw_routes(fig: go.Figure, routes: RouteSet, metrics: FlowMetrics, vehicles: set[VehicleType] | None,
                 row: int | None = None, col: int | None = None, legend: bool = True,
                 scale: tuple[float, float] | None = None, layout: Layout | None = None,
                 paths: dict[int, list[_Pt]] | None = None, labels: dict[str, dict] | None = None,
                 seen: set[VehicleType] | None = None) -> int:
    """Rota izleri (rota başına 1 çizgi + 1 ok izi), hareket rozetleri (annotation) ve kalınlık lejantı."""
    sp = _sp(row, col)
    moves = _cell_moves(metrics)
    shown = _shown(routes, vehicles)
    if not shown:
        return 0
    if paths is None:
        paths = (_offset_paths([r for _, r in shown], layout) if layout is not None
                 else {i: [(p.x, p.y) for p in r.points] for i, (_, r) in enumerate(shown)})
    fmin, fmax = scale if scale is not None else _freq_range(shown, moves)
    pair_vehicles = Counter((r.from_id, r.to_id) for r in routes.routes)
    seen = seen if seen is not None else set()
    badge_ids = {id(r) for _, r in sorted(
        shown, key=lambda nr: -(moves.get((nr[1].from_id, nr[1].to_id), 0.0) * nr[1].distance_m))[:MAX_BADGES]}
    blocked: list[_Box] = []
    if layout is not None:
        for s in layout.workstations:
            fp = s.footprint()
            blocked.append((fp.x - 0.3, fp.y - 0.3, fp.x2 + 0.3, fp.y2 + 0.3))
    blocked += [v["box"] for v in (labels or {}).values()]
    placed: list[_Pt] = []
    for i, (no, r) in enumerate(shown):
        f = moves.get((r.from_id, r.to_id), 0.0)
        color = VEHICLE_COLOR[r.vehicle]
        note = " (hücre toplamı; aynı çiftte birden fazla araç)" if pair_vehicles[(r.from_id, r.to_id)] > 1 else ""
        hover = (f"{r.from_id} → {r.to_id}<br>{VEHICLE_LABEL[r.vehicle]}<br>{f:g} hareket/sa{note}"
                 f"<br>{r.distance_m:.1f} m ({r.mode})<br>{f * r.distance_m:.0f} m/sa")
        first = r.vehicle not in seen
        seen.add(r.vehicle)
        w = _width(f, fmin, fmax)
        dash = "dash" if r.mode != "network" else ("dot" if f <= 0 else "solid")
        path = paths[i]
        fig.add_trace(go.Scatter(
            x=[q[0] for q in path], y=[q[1] for q in path], mode="lines",
            line={"color": color, "width": w, "dash": dash},
            opacity=0.9, name=VEHICLE_LABEL[r.vehicle], legendgroup=r.vehicle.value,
            showlegend=bool(first and legend), hoverinfo="text", text=hover), **sp)
        cum = _cum(path)
        total = cum[-1]
        badge_s = None
        if id(r) in badge_ids and total > 0:
            badge_s = _pick_badge(path, cum, placed, blocked)
            bx, by, _ = _at(path, cum, badge_s)
            placed.append((bx, by))
            fig.add_annotation(
                x=bx, y=by, text=f"<b>{no}</b>", showarrow=False, bgcolor="white", bordercolor=color,
                borderwidth=2, borderpad=1, font={"size": 11, "color": color}, name=f"move:{no}",
                hovertext=f"{no} · {r.from_id}→{r.to_id} · {f:g}/sa · {r.distance_m:.0f} m", **sp)
        if total > 1e-9:
            sel = _arrow_positions(total, badge_s, path, cum)
            pts = [_at(path, cum, s) for s in sel]
            fig.add_trace(go.Scatter(
                x=[p[0] for p in pts], y=[p[1] for p in pts], mode="markers",
                marker={"symbol": "arrow", "size": min(17.0, max(11.0, 8 + 0.8 * w)), "color": color,
                        "angleref": "up", "angle": [p[2] for p in pts], "line": {"color": "white", "width": 1}},
                hoverinfo="skip", showlegend=False), **sp)
    if legend:
        samples = ([(W_SINGLE, f"Tüm hareketler {fmax:g}/sa (eşit kalınlık)")] if fmax - fmin < 1e-9
                   else [(W_MIN, f"İnce = {fmin:g}/sa"), (W_MAX, f"Kalın = {fmax:g}/sa")])
        for wd, name in samples:
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode="lines", line={"color": _WIDTH_SAMPLE_COLOR, "width": wd},
                name=name, legendgroup="width", hoverinfo="skip", showlegend=True), **sp)
    return len(shown)


def _move_table(shown: list[tuple[int, Route]], moves: dict[tuple[str, str], float]) -> tuple[str, int]:
    lines = []
    for no, r in shown[:MAX_TABLE_ROWS]:
        f = moves.get((r.from_id, r.to_id), 0.0)
        lines.append(f"{no} · {r.from_id}→{r.to_id} · {VEHICLE_LABEL[r.vehicle]} · {f:g}/sa · "
                     f"{r.distance_m:.0f} m · {f * r.distance_m:.0f} m/sa")
    if len(shown) > MAX_TABLE_ROWS:
        lines.append(f"+{len(shown) - MAX_TABLE_ROWS} hareket daha (hover)")
    return "<br>".join(lines), len(lines)


def _draw_panel(fig: go.Figure, layout: Layout, routes: RouteSet, metrics: FlowMetrics,
                vehicles: set[VehicleType] | None, row: int | None = None, col: int | None = None,
                legend: bool = True, scale: tuple[float, float] | None = None,
                seen: set[VehicleType] | None = None) -> int:
    shown = _shown(routes, vehicles)
    paths = _offset_paths([r for _, r in shown], layout)
    labels = _draw_base(fig, layout, row, col, paths=list(paths.values()))
    return _draw_routes(fig, routes, metrics, vehicles, row, col, legend, scale, layout, paths, labels, seen)


# --------------------------------------------------------------------------- public API
def spaghetti_figure(layout: Layout, routes: RouteSet, metrics: FlowMetrics,
                     vehicles: set[VehicleType] | None = None, title: str = "") -> go.Figure:
    """Layout + istasyonlar + engeller + yönlü rota çizgileri (araç rengi, frekansla kalınlık, ok, hareket no)."""
    fig = go.Figure()
    _draw_panel(fig, layout, routes, metrics, vehicles)
    _style_axes(fig, layout)
    shown = _shown(routes, vehicles)
    legend_title = "Renk = araç · Kalınlık = hareket/sa · Ok = yön · No = hareket sırası"
    if routes.mode != "network":
        legend_title += " · Kesikli = yaklaşık (koridor yolu değil)"
    mode_note = "" if routes.mode == "network" else f" — {routes.mode} (gerçek koridor yolu DEĞİL)"
    bottom = 60
    if shown:
        text, nlines = _move_table(shown, _cell_moves(metrics))
        fig.add_annotation(text=text, xref="paper", yref="paper", x=0, y=-0.12, xanchor="left", yanchor="top",
                           align="left", showarrow=False, font={"size": 11}, name="move_table")
        bottom = 60 + 16 * nlines
    fig.update_layout(
        title=title or f"Spaghetti Chart — {layout.name} — D_total {metrics.total_distance_m_per_hour:.0f} m/sa{mode_note}",
        legend={"orientation": "h", "yref": "container", "y": 0, "yanchor": "bottom", "x": 0, "xanchor": "left",
                "title": {"text": legend_title, "side": "top"}},
        plot_bgcolor="white", margin={"l": 50, "r": 20, "t": 60, "b": bottom + 50})
    return fig


def heatmap_figure(layout: Layout, routes: RouteSet, metrics: FlowMetrics) -> go.Figure:
    """Izgara hücresi başına geçen hareket/saat (her rota hücreye en fazla bir kez sayılır)."""
    g = layout.facility.grid
    nx_, ny_ = math.ceil(layout.facility.width / g), math.ceil(layout.facility.height / g)
    z = [[0.0] * nx_ for _ in range(ny_)]
    moves = _cell_moves(metrics)
    for r in routes.routes:
        if not r.reachable or len(r.points) < 2:
            continue
        f = moves.get((r.from_id, r.to_id), 0.0)
        cells: set[tuple[int, int]] = set()
        for a, b in zip(r.points, r.points[1:], strict=False):
            n = max(1, int(math.dist((a.x, a.y), (b.x, b.y)) / (g / 4)))
            for k in range(n + 1):
                x = a.x + (b.x - a.x) * k / n
                y = a.y + (b.y - a.y) * k / n
                i, j = min(int(x // g), nx_ - 1), min(int(y // g), ny_ - 1)
                if 0 <= i < nx_ and 0 <= j < ny_:
                    cells.add((i, j))
        for i, j in cells:
            z[j][i] += f
    zz = [[v if v > 0 else None for v in row] for row in z]
    fig = go.Figure()
    fig.add_trace(go.Heatmap(
        z=zz, x=[(i + 0.5) * g for i in range(nx_)], y=[(j + 0.5) * g for j in range(ny_)],
        colorscale="YlOrRd", colorbar={"title": "hareket/sa"}, hoverongaps=False,
        hovertemplate="x=%{x:.1f} m, y=%{y:.1f} m<br>%{z:g} hareket/sa<extra></extra>"))
    real = [[(p.x, p.y) for p in r.points] for r in routes.routes if r.reachable and len(r.points) >= 2]
    _draw_base(fig, layout, paths=real)
    _style_axes(fig, layout)
    fig.update_layout(title=f"Akış yoğunluğu ısı haritası — {layout.name} ({routes.mode})",
                      plot_bgcolor="white", showlegend=False, margin={"l": 50, "r": 20, "t": 60, "b": 50})
    return fig


def before_after_figure(before: tuple[Layout, RouteSet, FlowMetrics], after: tuple[Layout, RouteSet, FlowMetrics]) -> go.Figure:
    b_l, b_r, b_m = before
    a_l, a_r, a_m = after
    d0, d1 = b_m.total_distance_m_per_hour, a_m.total_distance_m_per_hour
    delta = f"{(d1 - d0) / d0 * 100:+.1f}%" if d0 > 0 else "n/a"
    fig = make_subplots(rows=1, cols=2, subplot_titles=(
        f"Önce — {b_l.name}: {d0:.0f} m/sa", f"Sonra — {a_l.name}: {d1:.0f} m/sa ({delta})"))
    ranges = [rg for rg in (_freq_range(_shown(rs, None), _cell_moves(mt)) for rs, mt in ((b_r, b_m), (a_r, a_m)))
              if rg is not None]
    scale = (min(rg[0] for rg in ranges), max(rg[1] for rg in ranges)) if ranges else None
    seen: set[VehicleType] = set()
    for col, (lay, rs, mt) in enumerate(((b_l, b_r, b_m), (a_l, a_r, a_m)), start=1):
        _draw_panel(fig, lay, rs, mt, None, 1, col, legend=(col == 1), scale=scale, seen=seen)
        _style_axes(fig, lay, 1, col)
    fig.update_layout(
        title="Önce / Sonra — taşıma mesafesi karşılaştırması", plot_bgcolor="white",
        legend={"orientation": "h", "yref": "container", "y": 0, "yanchor": "bottom", "x": 0, "xanchor": "left",
                "title": {"text": "Renk = araç · Kalınlık = hareket/sa · Ok = yön · No = hareket sırası", "side": "top"}},
        margin={"l": 50, "r": 20, "t": 80, "b": 110})
    return fig


def layout_figure(layout: Layout, issues: list[ValidationIssue] | None = None) -> go.Figure:
    """Yalnız layout görünümü; çakışmalar kırmızı. issues None ise validate_layout ile hesaplanır."""
    if issues is None:
        from backend.layout.validation import validate_layout
        issues = validate_layout(layout).issues
    bad = {i for iss in issues for i in iss.entity_ids}
    fig = go.Figure()
    _draw_base(fig, layout, bad_ids=bad)
    _style_axes(fig, layout)
    n = len(issues)
    fig.update_layout(
        title=f"Layout — {layout.name}" + (f" — {n} sorun (kırmızı)" if n else " — sorun yok"),
        plot_bgcolor="white", showlegend=False, margin={"l": 50, "r": 20, "t": 60, "b": 50})
    return fig
