"""Isı haritası figür testleri: elle kurulmuş TimeGrid / FromToMatrix ile (hesap fonksiyonlarından bağımsız)."""
from __future__ import annotations

import os

import pytest

from backend.analytics.heatmap_figures import from_to_figure, time_grid_compare_figure, time_grid_figure
from backend.analytics.heatmaps import FromToMatrix, TimeGrid

NAMES = ["Kesim", "Büküm", "Kaynak", "Boya", "Montaj", "Test", "Paketleme"]


def make_grid(metric="utilization", n_st=7, n_bins=8, last=None, **kw) -> TimeGrid:
    edges = [3600.0 * j for j in range(n_bins + 1)]
    if last is not None:
        edges[-1] = edges[-2] + last
    vals = [[round(((i * 7 + j * 3) % 10) / 10 + 0.05, 2) for j in range(n_bins)] for i in range(n_st)]
    if metric != "utilization":
        vals = [[v * 6 for v in r] for r in vals]
    return TimeGrid(metric=metric, unit="oran (0–1)" if metric == "utilization" else "adet", layout_id="L",
                    station_ids=[f"S{i}" for i in range(n_st)], station_names=NAMES[:n_st],
                    bin_edges_s=edges, values=vals, **kw)


def make_ft(mode="network", n=7) -> FromToMatrix:
    mv = [[0.0] * n for _ in range(n)]
    dist: list[list[float | None]] = [[None] * n for _ in range(n)]
    for i in range(n - 1):
        mv[i][i + 1] = 12.5 + i * 3
        dist[i][i + 1] = 18.0 + i * 4.5
    mv[5][2] = 4.0
    dist[5][2] = 40.0
    mt = [[(mv[i][j] * dist[i][j]) if dist[i][j] else 0.0 for j in range(n)] for i in range(n)]
    return FromToMatrix(layout_id="L", mode=mode, station_ids=[f"S{i}" for i in range(n)], station_names=NAMES[:n],
                        moves_per_hour=mv, distance_m=dist, metres_per_hour=mt,
                        total_metres_per_hour=sum(map(sum, mt)))


def _hm(fig, k=0):
    return [t for t in fig.data if t.type == "heatmap"][k]


def _texts(fig):
    return " ".join(str(a.text) for a in fig.layout.annotations) + " " + str(fig.layout.title.text)


def test_util_exact_values_and_scale():
    g = make_grid()
    fig = time_grid_figure(g)
    h = _hm(fig)
    assert [list(r) for r in h.z] == g.values
    assert (h.zmin, h.zmax) == (0.0, 1.0)
    assert h.text[0][0] == f"%{round(g.values[0][0] * 100)}"
    assert list(h.x) == list(range(8)) and fig.layout.xaxis.ticktext[0] == "00:00–01:00"
    assert list(fig.layout.yaxis.ticktext) == NAMES
    assert fig.layout.yaxis.range[0] > fig.layout.yaxis.range[1]  # ilk istasyon üstte


def test_queue_scale_and_text():
    g = make_grid("avg_queue")
    h = _hm(time_grid_figure(g))
    assert h.zmin == 0 and h.zmax == max(v for r in g.values for v in r)
    assert h.text[1][1] == f"{g.values[1][1]:.1f}"
    gm = make_grid("max_queue")
    assert _hm(time_grid_figure(gm)).text[1][1] == str(round(gm.values[1][1]))
    assert _hm(time_grid_figure(g, zmax=99.0)).zmax == 99.0


def test_warmup_truncated_notes_visible():
    g = make_grid(warmup_s=7200.0, truncated=True, notes=["Kuyruk tanımı: X→Y"])
    fig = time_grid_figure(g)
    assert len(fig.layout.shapes) == 2  # bitişi <= warmup olan 2 dilim
    t = _texts(fig)
    assert "ısınma" in t and "UYARI" in t and "Kuyruk tanımı" in t
    clean = time_grid_figure(make_grid())
    assert len(clean.layout.shapes) == 0 and "UYARI" not in _texts(clean)


def test_short_last_bin_label():
    g = make_grid(n_bins=3, last=1800.0)
    assert list(time_grid_figure(g).layout.xaxis.ticktext)[-1] == "02:00–02:30"


def test_degenerate_grids():
    one = make_grid(n_st=1, n_bins=1)
    assert len(_hm(time_grid_figure(one)).z) == 1
    empty = TimeGrid(metric="utilization", unit="oran (0–1)", layout_id="L", station_ids=[], station_names=[],
                     bin_edges_s=[0.0], values=[])
    assert "veri yok" in _texts(time_grid_figure(empty)).lower()
    assert time_grid_compare_figure(empty, empty) is not None


def test_compare_common_scale_and_union_rows():
    a = make_grid("avg_queue", n_st=3)
    b = make_grid("avg_queue", n_st=5)
    b = b.model_copy(update={"values": [[v * 2 for v in r] for r in b.values]})
    fig = time_grid_compare_figure(a, b)
    h0, h1 = _hm(fig, 0), _hm(fig, 1)
    gmax = max(v for r in b.values for v in r)
    assert (h0.zmin, h0.zmax) == (h1.zmin, h1.zmax) == (0.0, gmax)
    assert len(h0.z) == len(h1.z) == 5
    assert all(v is None for v in h0.z[4])  # before'da olmayan istasyon boş
    assert [list(r) for r in h1.z] == b.values
    assert sum(1 for t in fig.data if t.showscale) == 1
    assert [x.text for x in fig.layout.annotations[:2]] == ["Önce", "Sonra"]
    u = time_grid_compare_figure(make_grid(), make_grid())
    assert (_hm(u, 0).zmax, _hm(u, 1).zmax) == (1.0, 1.0)


def test_compare_truncated_warning():
    fig = time_grid_compare_figure(make_grid(truncated=True), make_grid())
    assert "UYARI" in _texts(fig)


@pytest.mark.parametrize("value", ["moves", "metres", "distance"])
def test_from_to_values_and_blanks(value):
    m = make_ft()
    fig = from_to_figure(m, value)
    h = _hm(fig)
    src = {"moves": m.moves_per_hour, "metres": m.metres_per_hour, "distance": m.distance_m}[value]
    for i in range(7):
        for j in range(7):
            s = src[i][j]
            if s in (None, 0):
                assert h.z[i][j] is None and h.text[i][j] == ""
            else:
                assert h.z[i][j] == s and h.text[i][j] != ""
    assert fig.layout.xaxis.title.text == "Hedef" and fig.layout.yaxis.title.text == "Kaynak"
    assert fig.layout.yaxis.range[0] > fig.layout.yaxis.range[1]
    assert f"{m.total_metres_per_hour:.1f}" in _texts(fig)
    assert "yaklaşık" not in _texts(fig)


def test_from_to_non_network_warning_and_edge_cases():
    assert "yaklaşık" in _texts(from_to_figure(make_ft("manhattan")))
    with pytest.raises(ValueError):
        from_to_figure(make_ft(), "bogus")
    one = FromToMatrix(layout_id="L", mode="network", station_ids=["S0"], station_names=["A"], moves_per_hour=[[0.0]],
                       distance_m=[[None]], metres_per_hour=[[0.0]], total_metres_per_hour=0.0)
    assert _hm(from_to_figure(one)).z[0][0] is None
    none = one.model_copy(update={"station_ids": [], "station_names": [], "moves_per_hour": []})
    assert from_to_figure(none) is not None


def test_png_export(tmp_path):
    pytest.importorskip("kaleido")
    out = os.environ.get("HEATMAP_PNG_DIR") or str(tmp_path)
    os.makedirs(out, exist_ok=True)
    figs = {
        "util": time_grid_figure(make_grid(warmup_s=3600.0, notes=["Kullanım = işlem meşguliyeti / (kapasite × dilim)."])),
        "queue_trunc": time_grid_figure(make_grid("avg_queue", last=1800.0, truncated=True)),
        "compare": time_grid_compare_figure(make_grid("avg_queue", n_st=6), make_grid("avg_queue")),
        "ft_moves": from_to_figure(make_ft()),
        "ft_dist": from_to_figure(make_ft("euclidean"), "distance"),
    }
    for name, f in figs.items():
        p = os.path.join(out, f"{name}.png")
        f.write_image(p, width=1200, height=700, scale=2)
        assert os.path.getsize(p) > 5000
