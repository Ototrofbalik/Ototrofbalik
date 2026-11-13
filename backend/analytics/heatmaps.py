"""[Master sözleşmesi — DONDURULDU] Isı haritası verileri. Saf fonksiyonlar; tüm sayılar SimResult/FlowMetrics'ten.

Uygulayan: A ajanı (hesap). Figürler: backend/analytics/heatmap_figures.py (B ajanı).
"""
from __future__ import annotations

from math import ceil

from pydantic import Field

from backend.schemas import FlowMetrics, Layout, SimResult
from backend.schemas.common import Strict
from backend.schemas.simulation import EventType


class TimeGrid(Strict):
    """İstasyon × zaman dilimi matrisi. values[i][j] = station_ids[i], [bin_edges_s[j], bin_edges_s[j+1])."""
    metric: str                         # "utilization" | "avg_queue" | "max_queue"
    unit: str                           # "oran (0–1)" | "adet"
    layout_id: str
    station_ids: list[str]
    station_names: list[str]
    bin_edges_s: list[float]            # len = n_bins + 1; son dilim kısa olabilir
    values: list[list[float]]           # satır = istasyon, sütun = dilim
    warmup_s: float = 0.0               # ısınma dilimleri arayüzde taranır
    replication_shown: int = 0          # olay logu yalnız replikasyon 0
    truncated: bool = False             # olay logu kesildiyse son dilimler eksik
    notes: list[str] = Field(default_factory=list)


class FromToMatrix(Strict):
    """İstasyon × istasyon hareket matrisi (FlowMetrics.cells'ten). Hücre yoksa 0 / None."""
    layout_id: str
    mode: str                           # FlowMetrics.mode: network | euclidean | manhattan
    station_ids: list[str]              # akış sırasına göre (ürün rotası), sonra kalanlar
    station_names: list[str]
    moves_per_hour: list[list[float]]
    distance_m: list[list[float | None]]
    metres_per_hour: list[list[float]]
    total_metres_per_hour: float        # == FlowMetrics.total_distance_m_per_hour
    notes: list[str] = Field(default_factory=list)


def _edges(horizon_s: float, bin_s: float) -> list[float]:
    if not bin_s > 0:
        raise ValueError("Dilim uzunluğu (bin_s) sıfırdan büyük olmalıdır.")
    n = max(1, ceil(horizon_s / bin_s - 1e-9))
    return [min(k * bin_s, horizon_s) for k in range(n)] + [horizon_s]


def _truncated(sim: SimResult) -> bool:
    from backend.simulation import engine
    return len(sim.events) >= engine.MAX_EVENTS or any("kesildi" in n for n in sim.notes)


def _add_overlap(acc: list[float], edges: list[float], a: float, b: float) -> None:
    """[a,b] aralığının her dilimle kesişim süresini acc'e ekler."""
    if b <= a:
        return
    n = len(edges) - 1
    k = 0
    while k < n and edges[k + 1] <= a:
        k += 1
    while k < n and edges[k] < b:
        o = min(b, edges[k + 1]) - max(a, edges[k])
        if o > 0:
            acc[k] += o
        k += 1


def _intervals(sim: SimResult, start: EventType, end: EventType,
               t_end: float) -> dict[str, list[tuple[float, float]]]:
    """İstasyon başına (varlık, istasyon) start->end aralıkları; log sırası korunur; bitmeyen aralık t_end'de kapanır."""
    open_: dict[tuple[str, str], float] = {}
    out: dict[str, list[tuple[float, float]]] = {}
    for e in sim.events:
        if e.station_id is None or e.type not in (start, end):
            continue
        key = (e.entity_id, e.station_id)
        if e.type == start:
            open_[key] = e.t
        elif key in open_:
            out.setdefault(e.station_id, []).append((open_.pop(key), e.t))
    for (_, sid), t0 in open_.items():
        out.setdefault(sid, []).append((t0, t_end))
    return out


def _horizon(sim: SimResult) -> tuple[float, float, bool, list[str]]:
    """(ızgara ufku, bitmeyen aralıkların kapanış anı, kesik mi, notlar)."""
    trunc = _truncated(sim)
    horizon = float(sim.config.duration_s)
    notes: list[str] = []
    t_end = horizon
    if trunc:
        t_end = min(horizon, max((e.t for e in sim.events), default=0.0))
        notes.append(f"Olay logu kesildi: {t_end:.0f} s sonrası dilimler eksiktir (değerler olduğundan düşük "
                     "görünebilir); yorumlamadan önce dikkate alın.")
    return horizon, t_end, trunc, notes


def _common_notes(sim: SimResult) -> list[str]:
    notes = ["Olay logu yalnız replikasyon 0'dır; dilim değerleri replikasyon 0'a aittir."]
    if sim.config.replications > 1:
        notes.append(f"{sim.config.replications} replikasyon var; istasyon istatistikleri ortalamadır, bu nedenle tam ufuk "
                     "ağırlıklı ortalama yalnız replikasyon 0 ile karşılaştırılabilir.")
    if sim.config.warmup_s > 0:
        notes.append(f"Isınma {sim.config.warmup_s:.0f} s: ızgara 0..süre aralığını gösterir, ısınma dilimleri ham "
                     "değerdir; simülasyonun istasyon istatistikleri yalnız ısınma sonrası pencereden hesaplanır.")
    return notes


def station_time_utilization(sim: SimResult, layout: Layout, bin_s: float = 3600.0) -> TimeGrid:
    """Dilim içindeki işlem meşguliyeti / (kapasite × dilim uzunluğu). Tam ufukta ağırlıklı ortalama
    StationStats.utilization ile eşleşmeli (motorun tanımıyla aynı: setup dahil mi, warmup nasıl — motoru oku)."""
    horizon, t_end, trunc, extra = _horizon(sim)
    edges = _edges(horizon, bin_s)
    ivs = _intervals(sim, EventType.PROCESS_STARTED, EventType.PROCESS_FINISHED, t_end)
    values = []
    for ws in layout.workstations:
        acc = [0.0] * (len(edges) - 1)
        for a, b in ivs.get(ws.id, []):
            _add_overlap(acc, edges, max(a, 0.0), min(b, horizon))
        values.append([min(1.0, v / (ws.capacity * (edges[k + 1] - edges[k]))) for k, v in enumerate(acc)])
    notes = [
        "Doluluk = dilimdeki işlem meşguliyeti / (kapasite × dilim uzunluğu); meşguliyet, işlemin başlangıcından "
        "bitişine kadar geçen süredir (hazırlık dahil, simülasyonla aynı). Ufukta yarım kalan işlem süre sonunda "
        "kesilir.",
        "Son dilim kısa olabilir; her dilim kendi gerçek uzunluğuna göre normalize edilir.",
        *_common_notes(sim), *extra,
    ]
    return TimeGrid(metric="utilization", unit="oran (0–1)", layout_id=layout.id,
                    station_ids=[w.id for w in layout.workstations],
                    station_names=[w.name for w in layout.workstations],
                    bin_edges_s=edges, values=values, warmup_s=float(sim.config.warmup_s),
                    replication_shown=0, truncated=trunc, notes=notes)


def station_time_queue(sim: SimResult, layout: Layout, bin_s: float = 3600.0, stat: str = "avg") -> TimeGrid:
    """stat='avg': dilimde zaman ağırlıklı ortalama kuyruk; 'max': dilimdeki en büyük kuyruk.
    Kuyruk = QUEUE_ENTERED→PROCESS_STARTED aralığı (operatör bekleyen dahil); bu tanım notes'ta yazılır."""
    if stat not in ("avg", "max"):
        raise ValueError("stat 'avg' veya 'max' olmalıdır.")
    horizon, t_end, trunc, extra = _horizon(sim)
    edges = _edges(horizon, bin_s)
    nb = len(edges) - 1
    ivs = _intervals(sim, EventType.QUEUE_ENTERED, EventType.PROCESS_STARTED, t_end)
    values = []
    for ws in layout.workstations:
        spans = [(max(a, 0.0), min(b, horizon)) for a, b in ivs.get(ws.id, []) if min(b, horizon) > max(a, 0.0)]
        if stat == "avg":
            acc = [0.0] * nb
            for a, b in spans:
                _add_overlap(acc, edges, a, b)
            values.append([v / (edges[k + 1] - edges[k]) for k, v in enumerate(acc)])
        else:
            # Seviye = aynı andaki tüm olaylar işlendikten sonraki kuyruk uzunluğu; yalnız pozitif süre
            # süren seviyeler sayılır (anlık 0 s'lik girişler kuyruk sayılmaz).
            deltas: dict[float, int] = {}
            for a, b in spans:
                deltas[a] = deltas.get(a, 0) + 1
                deltas[b] = deltas.get(b, 0) - 1
            pts = sorted(deltas)
            mx = [0] * nb
            level = 0
            for i, t in enumerate(pts):
                level += deltas[t]
                t_next = pts[i + 1] if i + 1 < len(pts) else horizon
                if level <= 0 or t_next <= t:
                    continue
                k = 0
                while k < nb and edges[k + 1] <= t:
                    k += 1
                while k < nb and edges[k] < t_next:
                    mx[k] = max(mx[k], level)
                    k += 1
            values.append([float(v) for v in mx])
    what = "dilimde zaman ağırlıklı ortalama kuyruk uzunluğu" if stat == "avg" else "dilimdeki en büyük kuyruk uzunluğu"
    notes = [
        "Kuyruk = kuyruğa girişten işlem başlangıcına kadar bekleyen parti sayısı (istasyon kaynağını "
        f"bekleyenleri de, operatör bekleyenleri de içerir). Gösterilen: {what}.",
        "Simülasyonun raporladığı en büyük kuyruk değeri farklıdır: yalnız istasyon kaynağının kuyruğunu, giriş "
        "anında sayar; operatör bekleyen partileri saymaz. Bu yüzden ısı haritası ile istasyon istatistiklerindeki "
        "en büyük kuyruk birebir eşleşmeyebilir.",
        "Aynı anda gerçekleşen olaylar log sırasıyla işlenir; yalnız pozitif süre süren kuyruk seviyeleri sayılır.",
        *_common_notes(sim), *extra,
    ]
    return TimeGrid(metric="avg_queue" if stat == "avg" else "max_queue", unit="adet", layout_id=layout.id,
                    station_ids=[w.id for w in layout.workstations],
                    station_names=[w.name for w in layout.workstations],
                    bin_edges_s=edges, values=values, warmup_s=float(sim.config.warmup_s),
                    replication_shown=0, truncated=trunc, notes=notes)


def from_to_matrix(layout: Layout, metrics: FlowMetrics, order: list[str] | None = None) -> FromToMatrix:
    names = {w.id: w.name for w in layout.workstations}
    ids: list[str] = []
    seen: set[str] = set()

    def add(i: str) -> None:
        if i not in seen:
            seen.add(i)
            ids.append(i)

    for i in order or []:
        add(i)
    for c in metrics.cells:     # verilen order eksikse bile hücre uçları kaybolmaz
        add(c.from_id)
        add(c.to_id)
    for w in layout.workstations:
        add(w.id)
    idx = {i: k for k, i in enumerate(ids)}
    n = len(ids)
    moves = [[0.0] * n for _ in range(n)]
    metres = [[0.0] * n for _ in range(n)]
    dist: list[list[float | None]] = [[None] * n for _ in range(n)]
    for c in metrics.cells:
        r, q = idx[c.from_id], idx[c.to_id]
        moves[r][q] += c.moves_per_hour
        metres[r][q] += c.metres_per_hour
        dist[r][q] = c.distance_m
    notes = ["Veriler akış analizindeki istasyon çifti hücrelerinden gelir; hücre yoksa hareket 0, mesafe boştur (yok)."]
    if not order:
        notes.append("İstasyon sırası ürün rotası olarak layout'tan çıkarılamadı; hücrelerdeki ilk görülme sırası, "
                     "ardından kalan istasyonlar kullanıldı.")
    if metrics.mode != "network":
        notes.append(f"Mesafe modu '{metrics.mode}': mesafeler ağ rotası değil, yaklaşık mesafedir.")
    return FromToMatrix(layout_id=layout.id, mode=metrics.mode, station_ids=ids,
                        station_names=[names.get(i, i) for i in ids], moves_per_hour=moves, distance_m=dist,
                        metres_per_hour=metres, total_metres_per_hour=metrics.total_distance_m_per_hour, notes=notes)
