"""[Optimization Engineer] Arama algoritmaları: swap_search, local_search, simulated_annealing, ortools_assignment.

Ortak sözleşme: her algoritma baseline durumdan başlar, yalnız TAM değerlendirmede (validate_layout hatasız +
tüm akışlar erişilebilir) uygulanabilir ve kendi hedefine göre baseline'dan KESİN iyi durumları kaydeder
(`Tracker`). Rastgelelik yalnız `numpy.random.default_rng(seed)`; OR-Tools tek işçi + deterministik zaman
sınırı ile çalışır => aynı girdi + seed => aynı sonuç.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

import numpy as np
from ortools.sat.python import cp_model

from backend.layout.geometry import segment_interior_overlap
from backend.optimization.evaluator import Components, Evaluator, Objective
from backend.optimization.placement import (
    ROTATIONS,
    Placement,
    StandaloneInfo,
    State,
    center_of,
    footprint,
    pair_conflict,
    placement_from_center,
    state_key,
)
from backend.schemas import Rect, VehicleType

EPS = 1e-9
INF = math.inf
ALGORITHMS = ("swap_search", "local_search", "simulated_annealing", "ortools_assignment")


def _blocks_access(fp: Rect, info: StandaloneInfo) -> bool:
    """fp, istasyonun giriş VEYA çıkış stub adaylarının tamamını kesiyor mu? (ikili, muhafazakâr kontrol)"""
    def all_blocked(stubs: list) -> bool:
        return bool(stubs) and all(
            any(segment_interior_overlap(pl[i], pl[i + 1], fp) for i in range(len(pl) - 1)) for pl in stubs)
    return all_blocked(info.in_stubs) or all_blocked(info.out_stubs)


@dataclass
class Tracker:
    """Baseline'dan kesin iyi, birbirinden farklı en iyi K uygulanabilir durumu tutar."""
    baseline_obj: float
    k: int = 6
    items: list[tuple[float, State, dict[str, Placement]]] = field(default_factory=list)

    def add(self, obj: float, state: dict[str, Placement]) -> None:
        if not obj < self.baseline_obj - EPS:
            return
        key = state_key(state)
        if any(k == key for _, k, _ in self.items):
            return
        self.items.append((obj, key, dict(state)))
        self.items.sort(key=lambda t: (t[0], t[1]))
        del self.items[self.k:]


@dataclass
class RunOutput:
    algorithm: str
    results: list[tuple[float, dict[str, Placement]]]
    exact_evals: int
    notes: list[str] = field(default_factory=list)


class Search:
    def __init__(self, ev: Evaluator, base_state: dict[str, Placement], base_comps: Components) -> None:
        self.ev = ev
        self.base_comps = base_comps
        self.space = ev.space
        self.base = dict(base_state)
        self.movable = sorted(base_state)

    # ------------------------------------------------------------------ yardımcılar
    def _obj(self, objective: Objective, state: dict[str, Placement]) -> float:
        r = self.ev.exact(state)
        return objective(r.comps) if r.feasible else INF

    def _swap(self, state: dict[str, Placement], a: str, b: str) -> dict[str, Placement]:
        wa, wb = self.space.ws[a], self.space.ws[b]
        ca, cb = center_of(wa, state[a]), center_of(wb, state[b])
        s = dict(state)
        s[a] = placement_from_center(wa, cb[0], cb[1], state[a][2])
        s[b] = placement_from_center(wb, ca[0], ca[1], state[b][2])
        return s

    def _rotate(self, state: dict[str, Placement], sid: str, rot) -> dict[str, Placement]:
        ws = self.space.ws[sid]
        c = center_of(ws, state[sid])
        s = dict(state)
        s[sid] = placement_from_center(ws, c[0], c[1], rot)
        return s

    def _start(self, objective: Objective) -> tuple[dict[str, Placement], float, Tracker]:
        """Başlangıç = baseline. Tracker referansı baseline bileşenlerinden hesaplanan hedeftir (baseline
        geçersiz olsa bile tanımlı); mevcut hedef baseline uygulanamazsa INF."""
        return dict(self.base), self._obj(objective, self.base), Tracker(baseline_obj=objective(self.base_comps))

    # ------------------------------------------------------------------ swap_search
    def swap_search(self, objective: Objective, iterations: int) -> RunOutput:
        """En iyi-iyileştirme ikili değiş-tokuş (merkezler yer değiştirir, rotasyon korunur). Deterministik."""
        n0 = self.ev.n_exact
        cur, cur_obj, tr = self._start(objective)
        budget = iterations
        while budget > 0:
            best: tuple[float, dict[str, Placement]] | None = None
            for a, b in itertools.combinations(self.movable, 2):
                if budget <= 0:
                    break
                s = self._swap(cur, a, b)
                if not self.space.quick_ok(s, [a, b]):
                    continue
                budget -= 1
                o = self._obj(objective, s)
                if o < INF:
                    tr.add(o, s)
                    if best is None or o < best[0] - EPS:
                        best = (o, s)
            if best is None or not best[0] < cur_obj - EPS:
                break
            cur, cur_obj = best[1], best[0]
        return RunOutput("swap_search", [(o, s) for o, _, s in tr.items], self.ev.n_exact - n0)

    # ------------------------------------------------------------------ local_search
    def _neighbours(self, cur: dict[str, Placement]) -> list[tuple[tuple, dict[str, Placement], list[str]]]:
        out = []
        for sid in self.movable:
            for p in self.space.candidates.get(sid, []):
                if p == cur[sid]:
                    continue
                s = dict(cur)
                s[sid] = p
                out.append((("mv", sid, p[0], p[1], p[2].value), s, [sid]))
            for rot in ROTATIONS:
                if rot != cur[sid][2]:
                    out.append((("rot", sid, rot.value), self._rotate(cur, sid, rot), [sid]))
        for a, b in itertools.combinations(self.movable, 2):
            out.append((("sw", a, b), self._swap(cur, a, b), [a, b]))
        return out

    def local_search(self, objective: Objective, iterations: int, top_k: int = 12) -> RunOutput:
        """Komşuluk = yer değiştirme (ızgara+slot adayları, 4 rotasyon) + yerinde döndürme + swap.
        Komşular hızlı vekille (ağ-mesafe önbelleği) sıralanır; en iyi `top_k` tam değerlendirilir;
        en iyi tam iyileştirme kabul edilir (yerel optimuma kadar ya da tam-değerlendirme bütçesi bitene kadar)."""
        n0 = self.ev.n_exact
        cur, cur_obj, tr = self._start(objective)
        budget = iterations
        while budget > 0:
            scored = []
            for mk, s, changed in self._neighbours(cur):
                if not self.space.quick_ok(s, changed):
                    continue
                o = objective(self.ev.surrogate(s))
                if o < INF:
                    scored.append((o, mk, s))
            scored.sort(key=lambda t: (t[0], t[1]))
            best: tuple[float, dict[str, Placement]] | None = None
            for _, _, s in scored[:top_k]:
                if budget <= 0:
                    break
                budget -= 1
                o = self._obj(objective, s)
                if o < INF:
                    tr.add(o, s)
                    if best is None or o < best[0] - EPS:
                        best = (o, s)
            if best is None or not best[0] < cur_obj - EPS:
                break
            cur, cur_obj = best[1], best[0]
        return RunOutput("local_search", [(o, s) for o, _, s in tr.items], self.ev.n_exact - n0)

    # ------------------------------------------------------------------ simulated annealing
    def simulated_annealing(self, objective: Objective, iterations: int, seed: int) -> RunOutput:
        """Metropolis kabulü, geometrik soğuma (T0 = 5% · baseline hedefi -> T_end = 0.05%).
        Hamleler: %45 yer değiştirme (yarısı mevcut merkeze ≤6 m yakın adaylardan), %35 swap, %20 döndürme.
        Tüm değerlendirmeler tam (önbellekli); uygulanamaz aday reddedilir."""
        n0 = self.ev.n_exact
        rng = np.random.default_rng(seed)
        cur, cur_obj, tr = self._start(objective)
        ref = tr.baseline_obj if tr.baseline_obj < INF else 1.0
        t0, t_end = 0.05 * max(ref, 1e-6), 0.0005 * max(ref, 1e-6)
        alpha = (t_end / t0) ** (1.0 / max(iterations - 1, 1))
        temp = t0
        best_obj = cur_obj
        n = len(self.movable)
        for _ in range(iterations):
            u = rng.random()
            if u < 0.45 or n < 2:
                sid = self.movable[int(rng.integers(n))]
                cands = self.space.candidates.get(sid, [])
                if not cands:
                    temp *= alpha
                    continue
                if rng.random() < 0.5:
                    c = center_of(self.space.ws[sid], cur[sid])
                    near = [p for p in cands
                            if math.dist(center_of(self.space.ws[sid], p), c) <= 6.0 and p != cur[sid]]
                    pool = near or cands
                else:
                    pool = cands
                s = dict(cur)
                s[sid] = pool[int(rng.integers(len(pool)))]
                changed = [sid]
            elif u < 0.80:
                i, j = rng.choice(n, size=2, replace=False)
                a, b = self.movable[int(min(i, j))], self.movable[int(max(i, j))]
                s, changed = self._swap(cur, a, b), [a, b]
            else:
                sid = self.movable[int(rng.integers(n))]
                rots = [r for r in ROTATIONS if r != cur[sid][2]]
                s, changed = self._rotate(cur, sid, rots[int(rng.integers(len(rots)))]), [sid]
            r_acc = rng.random()
            if self.space.quick_ok(s, changed):
                o = self._obj(objective, s)
                if o < INF:
                    tr.add(o, s)
                    best_obj = min(best_obj, o)
                    delta = o - cur_obj
                    if cur_obj == INF or delta <= 0 or r_acc < math.exp(-delta / temp):
                        cur, cur_obj = s, o
            temp *= alpha
        out = RunOutput("simulated_annealing", [(o, s) for o, _, s in tr.items], self.ev.n_exact - n0)
        if best_obj < INF and not tr.items:
            out.notes.append("SA baseline'dan iyi uygulanabilir yerleşim bulamadı.")
        return out

    # ------------------------------------------------------------------ OR-Tools CP-SAT slot ataması
    def ortools_assignment(self, objective: Objective, seed: int, max_rounds: int = 5,
                           extra_slots: int | None = None, det_time: float = 3.0) -> RunOutput:
        """Kuadratik atama (QAP) CP-SAT ile: taşınabilir istasyonlar × (slot, rotasyon) seçenekleri.
        Slotlar = mevcut taşınabilir istasyon merkezleri + ızgaradan seçilmiş boş slotlar. Maliyet: akış kenarı
        başına frekans × vekil ağ mesafesi (istasyonun KENDİ ölçüsü/terminali ile), normalize ağırlıklarla
        (mesafe + yürüyüş + yakınlık). Alan bileşeni modele girmez (doğrusal değil) ama tam değerlendirmede
        hedefe dahildir. Çakışma/güvenlik boşluğu çiftleri x_a + x_b ≤ 1. Çözüm tam değerlendirilir; geçersizse
        (ör. başka istasyonun stub'ını kapatma) no-good kesiti eklenip yeniden çözülür."""
        n0 = self.ev.n_exact
        notes: list[str] = []
        cur, cur_obj, tr = self._start(objective)
        sp, ev = self.space, self.ev
        mov = self.movable
        slots = [center_of(sp.ws[s], self.base[s]) for s in mov]
        slots += self._extra_slots(len(mov) if extra_slots is None else extra_slots, slots)
        # seçenekler
        opts: dict[str, list[tuple[int, Placement]]] = {}
        for sid in mov:
            ws = sp.ws[sid]
            lst = []
            r0 = self.base[sid][2]
            rots = [r0, ROTATIONS[(ROTATIONS.index(r0) + 2) % 4]]   # mevcut + 180° (giriş/çıkış yer değişir)
            for si, (cx, cy) in enumerate(slots):
                for rot in rots:
                    p = placement_from_center(ws, cx, cy, rot)
                    if sp.info(sid, p).ok:
                        lst.append((si, p))
            if not lst:
                notes.append(f"OR-Tools: '{sid}' için hiçbir slotta uygulanabilir seçenek yok.")
                return RunOutput("ortools_assignment", [], ev.n_exact - n0, notes)
            opts[sid] = lst
        m = cp_model.CpModel()
        x = {(sid, k): m.NewBoolVar(f"x_{sid}_{k}") for sid in mov for k in range(len(opts[sid]))}
        for sid in mov:
            m.AddExactlyOne(x[sid, k] for k in range(len(opts[sid])))
        for si in range(len(slots)):
            m.AddAtMostOne(x[sid, k] for sid in mov for k, (s2, _) in enumerate(opts[sid]) if s2 == si)
        gap = sp.fac.safety_gap_m
        infos = {(sid, k): sp.info(sid, p) for sid in mov for k, (_, p) in enumerate(opts[sid])}
        fixed_infos = [sp.fixed_info(s.id) for s in sp.fixed]
        for sid in mov:   # sabit istasyonun tüm erişim stub'larını kapatan seçenek yasak
            for k in range(len(opts[sid])):
                if any(_blocks_access(infos[sid, k].fp, fi) for fi in fixed_infos):
                    m.Add(x[sid, k] == 0)
        for a, b in itertools.combinations(mov, 2):
            for ka, (sa, _) in enumerate(opts[a]):
                ia = infos[a, ka]
                for kb, (sb, _) in enumerate(opts[b]):
                    if sa == sb:
                        continue
                    ib = infos[b, kb]
                    if (pair_conflict(ia.fp, ib.fp, gap) or _blocks_access(ib.fp, ia)
                            or _blocks_access(ia.fp, ib)):
                        m.AddBoolOr([x[a, ka].Not(), x[b, kb].Not()])
        # maliyet katsayıları (normalize hedef ile uyumlu)
        bw, base = objective.w, objective.base
        per_m = {v: bw.distance * objective._n(1.0, base.distance) for v in VehicleType}
        per_m[VehicleType.OPERATOR] += bw.walking * objective._n(1.0, base.walking)
        adj_w = bw.adjacency * objective._n(1.0, base.adjacency)
        scale = 1e5
        terms = []
        flow_by_pair: dict[tuple[str, str], list] = {}
        for e in ev.edges:
            if e.from_id != e.to_id:
                flow_by_pair.setdefault((e.from_id, e.to_id), []).append(e)
        adj_by_pair: dict[tuple[str, str], float] = {}
        if adj_w > 0:
            for a, b in ev.adjacency_pairs:
                if a in sp.ws and b in sp.ws and a != b:
                    adj_by_pair[(a, b)] = adj_by_pair.get((a, b), 0.0) + adj_w

        def cost(a: str, pa: Placement | None, b: str, pb: Placement | None) -> float:
            st = dict(self.base)
            if pa is not None:
                st[a] = pa
            if pb is not None:
                st[b] = pb
            c = 0.0
            for e in flow_by_pair.get((a, b), []):
                d = ev.edge_dist(st, e)
                if d == INF:
                    return INF
                c += e.frequency_per_hour * per_m[e.vehicle] * d
            if (a, b) in adj_by_pair:
                c += adj_by_pair[(a, b)] * math.dist(ev._center(st, a), ev._center(st, b))
            return c

        for (a, b) in sorted(set(flow_by_pair) | set(adj_by_pair)):
            am, bm = a in opts, b in opts
            if am and bm:
                zs = []
                for ka, (_, pa) in enumerate(opts[a]):
                    for kb, (_, pb) in enumerate(opts[b]):
                        c = cost(a, pa, b, pb)
                        if c == INF:
                            continue
                        z = m.NewBoolVar(f"z_{a}_{ka}_{b}_{kb}")
                        m.AddImplication(z, x[a, ka])
                        m.AddImplication(z, x[b, kb])
                        zs.append(z)
                        terms.append((int(round(c * scale)), z))
                m.AddExactlyOne(zs)
            elif am or bm:
                mid = a if am else b
                for k, (_, p) in enumerate(opts[mid]):
                    c = cost(a, p if am else None, b, p if bm else None)
                    if c == INF:
                        m.Add(x[mid, k] == 0)
                    else:
                        terms.append((int(round(c * scale)), x[mid, k]))
        m.Minimize(sum(c * v for c, v in terms))
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = int(seed) % (2 ** 31)
        solver.parameters.max_deterministic_time = det_time
        for sid in mov:   # başlangıç ipucu: baseline ataması
            for k, (si, p) in enumerate(opts[sid]):
                m.AddHint(x[sid, k], si == mov.index(sid) and p == self.base[sid])
        for rnd in range(max_rounds):
            st = solver.Solve(m)
            if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                if rnd == 0:
                    notes.append(f"OR-Tools: model çözümsüz ({solver.StatusName(st)}).")
                break
            chosen = {}
            lits = []
            for sid in mov:
                for k, (_, p) in enumerate(opts[sid]):
                    if solver.BooleanValue(x[sid, k]):
                        chosen[sid] = p
                        lits.append(x[sid, k])
            if rnd == 0 and st == cp_model.FEASIBLE:
                notes.append("OR-Tools: deterministik zaman sınırında optimal kanıtlanamadı (FEASIBLE).")
            o = self._obj(objective, chosen)
            if o < INF:
                tr.add(o, chosen)
            m.AddBoolOr([lit.Not() for lit in lits])   # no-good: sonraki farklı atama
        return RunOutput("ortools_assignment", [(o, s) for o, _, s in tr.items], ev.n_exact - n0, notes)

    def _extra_slots(self, k: int, taken: list[tuple[float, float]]) -> list[tuple[float, float]]:
        """Izgara adaylarından (temsilî ilk taşınabilir istasyon ölçüsüyle) mevcut slotlarla ve birbirleriyle
        çakışmayan, koridora en kısa erişimli boş slot merkezleri (deterministik açgözlü seçim)."""
        if k <= 0 or not self.movable:
            return []
        sp = self.space
        rep = self.movable[0]
        ws = sp.ws[rep]
        gap = sp.fac.safety_gap_m
        taken_fps = [footprint(sp.ws[s], self.base[s]) for s in self.movable]
        scored = []
        for p in sp.candidates.get(rep, []):
            if p[2] != self.base[rep][2]:
                continue
            inf = sp.info(rep, p)
            acc = min(sum(math.dist(a, b) for a, b in itertools.pairwise(pl)) for pl in inf.in_stubs + inf.out_stubs)
            scored.append((acc, p[0], p[1], p))
        scored.sort(key=lambda t: t[:3])
        out: list[tuple[float, float]] = []
        fps = list(taken_fps)
        for _, _, _, p in scored:
            fp = footprint(ws, p)
            if any(pair_conflict(fp, f, gap) for f in fps):
                continue
            fps.append(fp)
            out.append(center_of(ws, p))
            if len(out) >= k:
                break
        return out
