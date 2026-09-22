"""Galactic Layout v2 (docs/GALACTIC_LAYOUT.md, фазы GL-1/GL-2) — чистая математика.

Астрофизическая раскладка Полной карты: каждая гранула получает ПОСТОЯННУЮ
позицию в map_layout по модели галактики (замена DrL, PLAN_FULL_MAP_3D §2.2):

    4 лог-спирали r=120·e^(0.28θ)  ← Fiedler-порядок графа кластеров (§2.1):
                                       связные кластеры встают рядом на рукаве
    балдж N(0, diag(90,90,65))      ← топ-64 кластеров по массе (§1.2)
    гало Пламмера                   ← полностью изолированные гранулы (§1.3)

Только numpy: плотный лапласиан G×G (G≈1.8k) — eigh копеечный, scipy/igraph
не нужны. RNG перноудовый, seed = FNV-1a(node_id) (§3): два force-прогона
побитово совпадают; знак Fiedler-вектора канонизирован — порядок не
переворачивается между машинами.

Пайплайн force (§5):
    assign_groups → cluster_adjacency → bulge топ-64 → fiedler_order →
    cut_into_arms → места (спираль/балдж) → члены/спутники/гало →
    relax_in_groups (§2.4)
Инкремент (§4): центроид размещённых членов → якорь по рёбрам → hash-слот
рукава → барицентр топ-3 соседей → гало; размещённые узлы не двигаются НИКОГДА.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from memory_server.memory.map_layout import relax_min_distance

# ── Космогония (§1): константы документа, единицы плана карты ──
R_DISK = 900.0                      # край диска (конец рукавов)
R_HALO = 1600.0                     # клип гало
SPIRAL_A = 120.0                    # r(θ) = A·e^(B·θ), pitch = arctan(B) ≈ 15.6°
SPIRAL_B = 0.28
THETA_MAX = float(np.log(R_DISK / SPIRAL_A) / SPIRAL_B)  # ≈7.2 рад (~1.15 витка)
N_ARMS = 4
ARM_TURN = 2.0 * np.pi / N_ARMS     # сдвиг рукава k = k·π/2
SIGMA_PERP = 55.0                   # поперёк рукава
SIGMA_PAR = 40.0                    # вдоль дуги
SIGMA_Z0 = 22.0                     # σ_z(r) = 22·(1+r/1400): 22 → 36 к краю (flare)
Z_FLARE_R = 1400.0
BULGE_TOP = 64                      # топ-кластеров по массе → балдж
BULGE_SIGMA = (90.0, 90.0, 65.0)
BULGE_MIN_DIST = 25.0
PLUMMER_RP = 600.0                  # гало: ρ ∝ (1+(r/r_p)²)^(-5/2)
HALO_U_RANGE = (0.05, 0.98)
SIGMA_CL_BASE = 14.0                # σ_cl(m) = 14 + 7·ln(max(2,m)), cap 60
SIGMA_CL_LOG = 7.0
SIGMA_CL_CAP = 60.0
CLUSTER_Z_SCALE = 0.6               # тонкий диск: z-джиттер членов ×0.6
SATELLITE_SIGMA = (120.0, 120.0, 60.0)  # поле вокруг якоря (§2.3)
NEIGHBOR_JITTER = 25.0              # барицентр соседей + N(0,25), z×0.5 (§4.3)

# ── Локальная релаксация §2.4 ──
RELAX_ITERS = 40
K_PLACE = 0.35
K_EDGE = 0.05
K_REPULSION = 400.0
REPULSION_CUTOFF = 30.0
VELOCITY_DAMPING = 0.85
RELAX_DT = 0.3

# Бюджет блока репульсии, элементов: (chunk, m, 3) float64 ≤ 24 МБ. Полная
# попарная матрица (m, m, 3) на гигантской ассоциации (инцидент прод-OOM
# 20.09: 5000 узлов → 600 МБ на массив, ~3 ГБ пик) — режем на блоки строк.
_RELAX_BLOCK_ELEMS = 1_000_000
# Свыше этого размера группы бюджет итераций линейно урезается: работа
# релаксации O(iters·m²), гигантская ассоциация 5000×40 итераций = минуты.
_RELAX_FULL_ITERS_SIZE = 2_048
_RELAX_MIN_ITERS = 8

# Регионы карты (лог-отчёт таски + метрики)
REGION_ARM = "arm"
REGION_BULGE = "bulge"
REGION_HALO = "halo"
REGION_SATELLITE = "satellite"
_ARM, _BULGE, _HALO, _SATELLITE = 0, 1, 2, 3

# Форма джиттера членов кластера: гаусс по осям × (1, 1, 0.6) — тонкий диск
_MEMBER_SCALE = np.array([1.0, 1.0, CLUSTER_Z_SCALE])
_SATELLITE_SCALE = np.array(SATELLITE_SIGMA)
_NEIGHBOR_SCALE = np.array([1.0, 1.0, 0.5])


def fnv1a32(text: str) -> int:
    """FNV-1a 32-bit — seed узла: побитово стабилен между прогонами
    (xxhash не в зависимостях, встроенный hash() рандомизируется PYTHONHASHSEED)."""
    h = 0x811C9DC5
    for byte in text.encode("utf-8"):
        h = ((h ^ byte) * 0x01000193) & 0xFFFFFFFF
    return h


def node_rng(seed_text: str) -> np.random.Generator:
    """Перноудовый RNG (§3): «коллапс волновой функции» звезды всегда даёт
    одну и ту же позицию — даже после TRUNCATE и полного пересева."""
    return np.random.default_rng(fnv1a32(seed_text))


@dataclass
class GalacticInput:
    """Вход раскладки: актуальные гранулы + резолвленные рёбра (только топология)."""

    node_ids: list[str]
    cluster_ids: list[str | None]
    importance: np.ndarray                     # (N,) float
    edges: np.ndarray                          # (E, 2) int64, глобальные индексы
    edge_weights: np.ndarray                   # (E,) float

    def __post_init__(self) -> None:
        self.importance = np.asarray(self.importance, dtype=np.float64)
        self.edges = np.asarray(self.edges, dtype=np.int64).reshape(-1, 2)
        self.edge_weights = np.asarray(self.edge_weights, dtype=np.float64)


@dataclass
class GalacticReport:
    """Счётчики прогона: лог-отчёт таски + метрики Prometheus (§7)."""

    mode: str                                              # full | incremental
    placed: int = 0
    regions: dict[str, int] = field(default_factory=dict)
    arm_mass: list[float] = field(default_factory=list)    # масса групп по рукавам
    arm_balance_pct: float | None = None                   # (max−min)/mean, %
    edge_median_len: float | None = None                   # медиана рёбер после прогона


def _region_counts(region_of: np.ndarray) -> dict[str, int]:
    return {
        region: int((region_of == code).sum())
        for region, code in (
            (REGION_ARM, _ARM),
            (REGION_BULGE, _BULGE),
            (REGION_HALO, _HALO),
            (REGION_SATELLITE, _SATELLITE),
        )
    }


def _arm_balance(arm_mass: np.ndarray) -> float | None:
    """Разброс массы рукавов, % от среднего (§8.3: ≤20% — нет «пустого рукава»)."""
    mean = float(arm_mass.mean()) if len(arm_mass) else 0.0
    return None if mean <= 0.0 else 100.0 * float(np.ptp(arm_mass)) / mean


def _median_edge_len(coords: np.ndarray, inp: GalacticInput) -> float | None:
    """Медиана длин резолвленных рёбер между размещёнными узлами (§8.3)."""
    if not len(inp.edges):
        return None
    src, tgt = inp.edges[:, 0], inp.edges[:, 1]
    known = ~np.isnan(coords[src, 0]) & ~np.isnan(coords[tgt, 0])
    if not known.any():
        return None
    spans = np.linalg.norm(coords[src[known]] - coords[tgt[known]], axis=1)
    return float(np.median(spans))


# ══════════════════════════════════════════════════════════════════
# Космогония: места (§1)
# ══════════════════════════════════════════════════════════════════


def sigma_z(radius: float) -> float:
    """Толщина диска с flare (§1.1)."""
    return SIGMA_Z0 * (1.0 + radius / Z_FLARE_R)


def cluster_sigma(size: int) -> float:
    """Гаусс кластера-сгустка (§2.2): логарифмический — большие не раздуваются."""
    return float(min(SIGMA_CL_CAP, SIGMA_CL_BASE + SIGMA_CL_LOG * np.log(max(2, size))))


def arm_place(rng: np.random.Generator, theta: float, arm: int) -> np.ndarray:
    """Точка лог-спирали + дисперсии §1.1 в локальном базисе (радиаль/тангенс/z)."""
    radius = SPIRAL_A * float(np.exp(SPIRAL_B * theta))
    angle = theta + arm * ARM_TURN
    radial = np.array([np.cos(angle), np.sin(angle), 0.0])
    tangent = np.array([-np.sin(angle), np.cos(angle), 0.0])
    return (
        radial * (radius + rng.normal(0.0, SIGMA_PERP))
        + tangent * rng.normal(0.0, SIGMA_PAR)
        + np.array([0.0, 0.0, rng.normal(0.0, sigma_z(radius))])
    )


def bulge_places(count: int) -> np.ndarray:
    """Места хабов (§1.2): трёхосная гауссиана + разлёт пар ближе 25."""
    if count <= 0:
        return np.zeros((0, 3))
    rng = np.random.default_rng(fnv1a32("galactic:bulge"))
    places = rng.normal(0.0, np.array(BULGE_SIGMA), size=(count, 3))
    return relax_min_distance(places, BULGE_MIN_DIST, iterations=24)


def halo_point(rng: np.random.Generator) -> np.ndarray:
    """Гало одиночек (§1.3): Пламмер, инверсия кумулятивной массы, изотропия."""
    u = rng.uniform(*HALO_U_RANGE)
    radius = PLUMMER_RP * u ** (1.0 / 3.0) / np.sqrt(max(1e-12, 1.0 - u ** (2.0 / 3.0)))
    direction = rng.normal(size=3)
    norm = float(np.linalg.norm(direction)) or 1.0
    return direction / norm * min(R_HALO, radius)


def hash_arm_slot(key: str) -> tuple[int, float]:
    """(arm, s) для изолированной группы (§2.1 п.3): детерминированный досев."""
    h = fnv1a32(key)
    return h % N_ARMS, ((h >> 8) % 4096) / 4095.0


# ══════════════════════════════════════════════════════════════════
# Структура: группы и порядок вдоль рукавов (§2.1)
# ══════════════════════════════════════════════════════════════════


def assign_groups(inp: GalacticInput) -> tuple[np.ndarray, list[str]]:
    """Кластеры + виртуальные ассоциации → (group_of (N,), group_keys).

    Ассоциация — компонента связности свободных гранул размера ≥2 по их
    взаимным рёбрам (union-find); ключ "assoc:<min node_id>" стабилен между
    прогонами. Свободная вне ассоциаций — спутник/гало (group_of = −1).
    """
    cluster_keys = sorted({c for c in inp.cluster_ids if c})
    cluster_index = {k: i for i, k in enumerate(cluster_keys)}
    group_of = np.array(
        [cluster_index.get(c, -1) for c in inp.cluster_ids], dtype=np.int64
    )
    if not (group_of < 0).any() or not len(inp.edges):
        return group_of, cluster_keys

    free_nodes = set(np.flatnonzero(group_of < 0).tolist())
    parent = {i: i for i in free_nodes}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # path halving
            x = parent[x]
        return x

    for u, v in zip(inp.edges[:, 0].tolist(), inp.edges[:, 1].tolist()):
        if u in free_nodes and v in free_nodes:
            parent[find(u)] = find(v)
    components: dict[int, list[int]] = {}
    for i in free_nodes:
        components.setdefault(find(i), []).append(i)
    associations = sorted(
        (members for members in components.values() if len(members) >= 2),
        key=lambda members: min(inp.node_ids[m] for m in members),
    )
    for offset, members in enumerate(associations, start=len(cluster_keys)):
        group_of[members] = offset
        cluster_keys.append(f"assoc:{min(inp.node_ids[m] for m in members)}")
    return group_of, cluster_keys


def cluster_adjacency(
    group_of: np.ndarray, inp: GalacticInput, n_groups: int
) -> np.ndarray:
    """Плотная симметричная W (G×G): вес ребра групп = Σ весов рёбер их гранул."""
    weights = np.zeros((n_groups, n_groups))
    if not len(inp.edges) or n_groups == 0:
        return weights
    g_u = group_of[inp.edges[:, 0]]
    g_v = group_of[inp.edges[:, 1]]
    cross = (g_u >= 0) & (g_v >= 0) & (g_u != g_v)
    gu, gv, w = g_u[cross], g_v[cross], inp.edge_weights[cross]
    np.add.at(weights, (gu, gv), w)
    np.add.at(weights, (gv, gu), w)
    return weights


def fiedler_order(adjacency: np.ndarray) -> np.ndarray:
    """Порядок вершин СВЯЗНОГО графа по Fiedler-вектору (§2.1): собственный
    вектор лапласиана при λ₂ минимизирует сумму длин рёбер на прямой.

    Знак вектора канонизирован: LAPACK его не определяет, без фиксации
    порядок переворачивался бы между машинами/сборками BLAS.
    """
    connected = np.flatnonzero(adjacency.sum(axis=1) > 0)
    if len(connected) < 2:
        return connected
    sub = adjacency[np.ix_(connected, connected)]
    laplacian = np.diag(sub.sum(axis=1)) - sub
    _, eigvecs = np.linalg.eigh(laplacian)
    fiedler = eigvecs[:, 1]
    pivot = int(np.argmax(np.abs(fiedler)))
    return connected[np.argsort(fiedler if fiedler[pivot] >= 0 else -fiedler, kind="stable")]


def spectral_order(adjacency: np.ndarray) -> np.ndarray:
    """Порядок рукавных вершин: компоненты по убыванию размера (ties → меньший
    индекс), внутри каждой — Fiedler.

    Живой корпус несвязен (балдж вынимает хабы, граф кластеров рвётся): без
    сортировки компонент нулевые стыки попадают в начало порядка и жадный
    порез кромсает голову, оставляя один гигантский рукав.
    """
    degree = adjacency.sum(axis=1)
    visited = np.zeros(len(adjacency), dtype=bool)
    components: list[np.ndarray] = []
    for start in np.flatnonzero(degree > 0):
        if visited[start]:
            continue
        visited[start] = True
        stack, members = [int(start)], [int(start)]
        while stack:
            vertex = stack.pop()
            for nxt in np.flatnonzero(adjacency[vertex] > 0):
                if not visited[nxt]:
                    visited[nxt] = True
                    stack.append(int(nxt))
                    members.append(int(nxt))
        components.append(np.array(sorted(members), dtype=np.int64))
    components.sort(key=lambda m: (-len(m), int(m[0])))
    order: list[int] = []
    for members in components:
        order.extend(members[fiedler_order(adjacency[np.ix_(members, members)])].tolist())
    return np.array(order, dtype=np.int64)


def cut_into_arms(
    order: np.ndarray,
    adjacency: np.ndarray,
    vertex_mass: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Порез Fiedler-порядка на N_ARMS сегментов по минимумам связности (§2.1).

    Целевые позиции — четверти кумулятивной МАССЫ порядка (критерий §8.3 —
    баланс массы, не вершин); в окне вокруг цели берётся локальный минимум
    cut[i] = Σ весов рёбер через разрез i|i+1 (префикс-суммой событий рёбер,
    дешевле наивного O(n²) перебора разрезов). Возвращает (arm_of, s):
    номер рукава 0..3 и ранг s∈[0,1] для вершин порядка; −1/0 — не вошедшим.
    """
    n = len(order)
    arm_of = np.full(len(adjacency), -1, dtype=np.int64)
    slot = np.zeros(len(adjacency))
    if n == 0:
        return arm_of, slot
    if n <= N_ARMS:
        arm_of[order] = np.arange(n)
        slot[order] = 0.5
        return arm_of, slot

    ranks = np.full(len(adjacency), -1, dtype=np.int64)
    ranks[order] = np.arange(n)
    row, col = np.nonzero(np.triu(adjacency, k=1))
    lo = np.minimum(ranks[row], ranks[col])
    hi = np.maximum(ranks[row], ranks[col])
    diff = np.zeros(n)
    np.add.at(diff, lo, adjacency[row, col])
    np.add.at(diff, hi, -adjacency[row, col])
    cut = np.cumsum(diff)[:-1]
    # Разрезы: окно ±5% четверти по ОСИ КУМУЛЯТИВНОЙ МАССЫ (позиционные окна
    # на степенном корпусе промахивались мимо цели: масса на вершину гуляет
    # впятеро между зонами порядка, ±42 позиции = ±40% массы). В окне берём
    # слабейший разрез, слегка штрафуя остаточный перекос массы — баланс
    # рукавов (§8.3) важнее идеального минимума связности.
    mass_order = np.ones(len(adjacency)) if vertex_mass is None else vertex_mass
    cumulative = np.cumsum(mass_order[order])
    quarter = float(cumulative[-1]) / N_ARMS
    norm_cut = cut / (float(cut.max()) + 1e-9)
    min_gap = max(1, n // (N_ARMS * 2))
    cuts: list[int] = []
    for k in range(1, N_ARMS):
        target = quarter * k
        slack = 0.05 * quarter
        lo_w = int(np.searchsorted(cumulative, target - slack))
        hi_w = min(n - 2, int(np.searchsorted(cumulative, target + slack)) - 1)
        if hi_w < lo_w:  # хаб массой шире окна перекрыл цель — ближайшие позиции
            mid = int(np.searchsorted(cumulative, target))
            lo_w, hi_w = max(0, mid - 1), min(n - 2, mid)
        window = np.arange(max(0, lo_w), hi_w + 1)
        score = norm_cut[window] + 4.0 * np.abs(cumulative[window] - target) / quarter
        allowed = [p for p in window[np.argsort(score, kind="stable")].tolist()
                   if all(abs(p - c) >= min_gap for c in cuts)]
        fallback = min(max(int(np.searchsorted(cumulative, target)), 0), n - 2)
        cuts.append(allowed[0] if allowed else fallback)
    bounds = np.concatenate(([-1], sorted(cuts), [n - 1]))
    for arm in range(N_ARMS):
        segment = order[bounds[arm] + 1 : bounds[arm + 1] + 1]
        arm_of[segment] = arm
        slot[segment] = np.linspace(0.0, 1.0, len(segment)) if len(segment) > 1 else 0.5
    return arm_of, slot


# ══════════════════════════════════════════════════════════════════
# Население (§2) и инкремент (§4)
# ══════════════════════════════════════════════════════════════════


def _group_members(group_of: np.ndarray, n_groups: int) -> list[np.ndarray]:
    """Члены каждой группы одним argsort: G проходов flatnonzero(group==g) —
    это O(G·N) ≈ 26M операций на живом корпусе, тут O(N log N)."""
    if n_groups == 0:
        return []
    order = np.argsort(group_of, kind="stable")
    sorted_groups = group_of[order]
    idx = np.arange(n_groups)
    starts = np.searchsorted(sorted_groups, idx, side="left")
    ends = np.searchsorted(sorted_groups, idx, side="right")
    return [order[s:e] for s, e in zip(starts.tolist(), ends.tolist())]


def _group_edge_indices(
    group_of: np.ndarray, inp: GalacticInput, n_groups: int
) -> list[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Рёбра внутри групп: (li, lj, w) с ЛОКАЛЬНЫМИ индексами членов, один
    проход по рёбрам — маска edges==g на каждую группу стоила бы O(G·E)."""
    empty = (
        np.empty(0, np.int64),
        np.empty(0, np.int64),
        np.empty(0, np.float64),
    )
    result: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = [empty] * n_groups
    if not len(inp.edges) or n_groups == 0:
        return result
    g_u = group_of[inp.edges[:, 0]]
    g_v = group_of[inp.edges[:, 1]]
    inner = np.flatnonzero((g_u == g_v) & (g_u >= 0))
    if inner.size == 0:
        return result
    # локальный индекс узла = позиция внутри его блока stable-argsort по группам
    order = np.argsort(group_of, kind="stable")
    sorted_g = group_of[order]
    new_block = np.ones(len(order), dtype=bool)
    new_block[1:] = sorted_g[1:] != sorted_g[:-1]
    starts = np.flatnonzero(new_block)
    local_idx = np.empty(len(order), dtype=np.int64)
    local_idx[order] = np.arange(len(order)) - starts[np.cumsum(new_block) - 1]
    # группировка inner-рёбер по группе концов
    edge_groups = g_u[inner]
    by_group = np.argsort(edge_groups, kind="stable")
    grouped = edge_groups[by_group]
    lo = np.searchsorted(grouped, np.unique(grouped), side="left")
    hi = np.searchsorted(grouped, np.unique(grouped), side="right")
    for g, s, e in zip(np.unique(grouped).tolist(), lo.tolist(), hi.tolist()):
        ids = inner[by_group[s:e]]
        result[g] = (
            local_idx[inp.edges[ids, 0]],
            local_idx[inp.edges[ids, 1]],
            inp.edge_weights[ids],
        )
    return result


def _group_masses(
    group_of: np.ndarray, importance: np.ndarray, n_groups: int
) -> np.ndarray:
    """mass = member_count·(1+mean_importance) (§1.2) — ранг хаба для балджа."""
    sizes = np.bincount(group_of[group_of >= 0], minlength=n_groups).astype(np.float64)
    imp_sum = np.zeros(n_groups)
    np.add.at(imp_sum, group_of[group_of >= 0], importance[group_of >= 0])
    return sizes * (1.0 + imp_sum / np.maximum(sizes, 1.0))


def _satellite_anchors(
    group_of: np.ndarray, inp: GalacticInput, n_groups: int
) -> dict[int, int]:
    """Спутник → якорь: группа с максимумом Σ весов рёбер к её членам (§2.3)."""
    free = group_of < 0
    g_u = group_of[inp.edges[:, 0]]
    g_v = group_of[inp.edges[:, 1]]
    fwd = free[inp.edges[:, 0]] & ~free[inp.edges[:, 1]]
    bwd = ~free[inp.edges[:, 0]] & free[inp.edges[:, 1]]
    node = np.concatenate((inp.edges[fwd, 0], inp.edges[bwd, 1]))
    anchor = np.concatenate((g_v[fwd], g_u[bwd]))
    if node.size == 0:
        return {}
    # агрегируем (node, group) → Σw, затем argmax Σw внутри узла
    pair_ids = node * n_groups + anchor
    uniq, inverse = np.unique(pair_ids, return_inverse=True)
    sums = np.zeros(len(uniq))
    np.add.at(sums, inverse, np.concatenate((inp.edge_weights[fwd], inp.edge_weights[bwd])))
    best = np.lexsort((-sums, uniq // n_groups))  # внутри узла — по убыванию Σw
    first = np.ones(len(best), dtype=bool)
    ordered_nodes = uniq[best] // n_groups
    first[1:] = ordered_nodes[1:] != ordered_nodes[:-1]
    return {
        int(pair // n_groups): int(pair % n_groups) for pair in uniq[best[first]]
    }


def relax_in_groups(
    coords: np.ndarray,
    group_of: np.ndarray,
    inp: GalacticInput,
    n_groups: int,
    iterations: int = RELAX_ITERS,
) -> np.ndarray:
    """Локальная spring-electrical релаксация внутри групп (§2.4).

    Глобальную структуру держит спираль — глобальной репульсии нет; пружина
    к месту посадки не даёт группе расползтись. Репульсия считается блоками
    строк (память O(chunk·m), не O(m²) — прод-OOM: ассоциация 5000 давала
    (m, m, 3) = 600 МБ × несколько массивов). d клиппируется снизу единицей:
    Ньютон 400/d² при d→0 взрывает скорости, а единица на масштабе сотен
    неотличима. Symplectic Euler (v-первый) — стабильнее явного.
    """
    coords = coords.copy()
    for members, (li, lj, w) in zip(
        _group_members(group_of, n_groups),
        _group_edge_indices(group_of, inp, n_groups),
    ):
        m = len(members)
        if m < 2:
            continue
        x = coords[members]
        place = x.copy()
        velocity = np.zeros_like(x)
        row_chunk = m if m <= 512 else max(1, _RELAX_BLOCK_ELEMS // (3 * m))
        # бюджет итераций гигантов урезаем: релаксация — полировка посадки,
        # суммарная работа O(iters·m²); 40 полных итераций писались для m≤200
        budget = iterations
        if m > _RELAX_FULL_ITERS_SIZE:
            budget = max(_RELAX_MIN_ITERS, iterations * _RELAX_FULL_ITERS_SIZE // m)
        for _ in range(budget):
            force = K_PLACE * (place - x)
            for start in range(0, m, row_chunk):
                stop = min(start + row_chunk, m)
                diff = x[start:stop, None, :] - x[None, :, :]  # (b, m, 3)
                dist = np.sqrt(np.einsum("bmi,bmi->bm", diff, diff))
                # диагональ: строка k блока ↔ колонка start+k (колонки глобальны)
                dist[np.arange(stop - start), np.arange(start, stop)] = np.inf
                near_rows, near_cols = np.nonzero(dist < REPULSION_CUTOFF)
                if near_rows.size:
                    # сила только по ближним парам: where-маскирование всех
                    # (b, m, 3) пар тратило впустую большую часть flops
                    safe3 = np.maximum(dist[near_rows, near_cols], 1.0) ** 3
                    force[start + near_rows] += (
                        K_REPULSION * diff[near_rows, near_cols] / safe3[:, None]
                    )
            if len(w):
                edge_diff = x[lj] - x[li]
                edge_d = np.maximum(np.linalg.norm(edge_diff, axis=1), 1.0)
                pull = K_EDGE * w[:, None] * edge_diff / edge_d[:, None]
                np.add.at(force, li, pull)
                np.add.at(force, lj, -pull)
            velocity *= VELOCITY_DAMPING
            velocity += force * RELAX_DT
            x += velocity * RELAX_DT
            if float(np.abs(velocity).max()) < 1e-3:
                break
        coords[members] = x
    return coords


def layout_full(inp: GalacticInput) -> tuple[np.ndarray, GalacticReport]:
    """Полный пересев галактики (force, §5): все N узлов, побитовый детерминизм."""
    n_nodes = len(inp.node_ids)
    coords = np.full((n_nodes, 3), np.nan)
    region_of = np.full(n_nodes, -1, dtype=np.int64)
    group_of, group_keys = assign_groups(inp)
    n_groups = len(group_keys)
    arm_mass = np.zeros(N_ARMS)

    if n_groups:
        mass = _group_masses(group_of, inp.importance, n_groups)
        adjacency = cluster_adjacency(group_of, inp, n_groups)
        bulge = np.argsort(-mass, kind="stable")[: min(BULGE_TOP, n_groups)]
        bulge_set = set(bulge.tolist())
        centers = np.full((n_groups, 3), np.nan)
        centers[bulge] = bulge_places(len(bulge))

        # рукава: Fiedler-порядок не-балдж вершин подграфа, порез по минимумам
        arm_vertices = np.array(
            [g for g in range(n_groups) if g not in bulge_set], dtype=np.int64
        )
        if len(arm_vertices):
            sub = adjacency[np.ix_(arm_vertices, arm_vertices)]
            # NB: подграф индексирует массы локально — mass[arm_vertices]
            arm_local, slot_local = cut_into_arms(spectral_order(sub), sub, mass[arm_vertices])
            arm_global = np.full(n_groups, -1, dtype=np.int64)
            arm_global[arm_vertices] = arm_local
            for local_pos, g in enumerate(arm_vertices):
                if arm_local[local_pos] >= 0:
                    arm = int(arm_local[local_pos])
                    centers[g] = arm_place(
                        node_rng("arm:" + group_keys[g]),
                        slot_local[local_pos] * THETA_MAX,
                        arm,
                    )
                    arm_mass[arm] += mass[g]
        # изолированные в подграфе рукавов (нет межкластерных рёбер, кроме
        # разве что к балджу): рукав — жадный баланс массы (§2.1 п.3 «равно-
        # мерно и детерминированно»: hash-рукав давал биномиальный перекос
        # ±40%), слот s внутри рукава — hash ключа группы
        for g in range(n_groups):
            if np.isnan(centers[g, 0]):
                arm = int(np.argmin(arm_mass))
                _, s = hash_arm_slot("iso:" + group_keys[g])
                centers[g] = arm_place(
                    node_rng("iso:" + group_keys[g]), s * THETA_MAX, arm
                )
                arm_mass[arm] += mass[g]

        # члены кластеров/ассоциаций: сгустки вокруг центров (§2.2)
        region_of[group_of >= 0] = _ARM
        region_of[np.isin(group_of, bulge)] = _BULGE
        for g, members in enumerate(_group_members(group_of, n_groups)):
            sigma = cluster_sigma(len(members))
            for m in members.tolist():
                rng = node_rng(inp.node_ids[m])
                coords[m] = centers[g] + rng.normal(0.0, sigma, size=3) * _MEMBER_SCALE

        # спутники: свободные с рёбрами к группам — поле вокруг якоря (§2.3)
        for node, anchor in _satellite_anchors(group_of, inp, n_groups).items():
            rng = node_rng(inp.node_ids[node])
            coords[node] = centers[anchor] + rng.normal(size=3) * _SATELLITE_SCALE
            region_of[node] = _SATELLITE

        coords = relax_in_groups(coords, group_of, inp, n_groups)

    # одиночки без рёбер — гало Пламмера (§1.3); всегда последними: все
    # прочие пути уже пометили регион
    for node in np.flatnonzero(region_of < 0):
        coords[node] = halo_point(node_rng(inp.node_ids[node]))
        region_of[node] = _HALO

    bad = ~np.isfinite(coords)
    if bad.any():
        coords[bad] = 0.0  # страховка INSERT: NaN в REAL испортил бы карту навсегда
    report = GalacticReport(
        mode="full",
        placed=n_nodes,
        regions=_region_counts(region_of),
        arm_mass=arm_mass.tolist(),
        arm_balance_pct=_arm_balance(arm_mass),
        edge_median_len=_median_edge_len(coords, inp),
    )
    return coords, report


def layout_increment(
    inp: GalacticInput, placed_coords: np.ndarray
) -> tuple[np.ndarray, np.ndarray, GalacticReport]:
    """Инкрементальная посадка (§4): только узлы без координат, старые не двигаются.

    Правила по приоритету документа: (1) кластер с размещёнными членами → их
    центроид + σ_cl; (2) новый кластер/ассоциация → место у якоря по рёбрам
    (σ_perp/σ_z) или hash-слот рукава; (3) свободная с размещёнными соседями →
    взвешенный барицентр топ-3 + N(0,25); (4) полный одиночка → гало.
    Возвращает (todo_indices, coords_todo, report).
    """
    placed_coords = np.asarray(placed_coords, dtype=np.float64)
    todo = np.flatnonzero(np.isnan(placed_coords[:, 0]))
    region_of = np.full(len(todo), -1, dtype=np.int64)
    coords = np.full((len(todo), 3), np.nan)
    group_of, group_keys = assign_groups(inp)
    n_groups = len(group_keys)

    has_placed = np.zeros(n_groups, dtype=bool)
    centroids = np.full((n_groups, 3), np.nan)
    members_by_group = _group_members(group_of, n_groups)
    for g, members in enumerate(members_by_group):
        placed = placed_coords[members]
        seated = ~np.isnan(placed[:, 0])
        if seated.any():
            has_placed[g] = True
            centroids[g] = placed[seated].mean(axis=0)
    sizes = np.array([len(m) for m in members_by_group], dtype=np.int64)
    adjacency = cluster_adjacency(group_of, inp, n_groups)

    # §4.2: места новых групп (нет размещённых членов) — якорь по рёбрам или
    # hash-слот; ОДНО место на группу, члены вокруг него — кластер не разъедется
    todo_index = {int(i): pos for pos, i in enumerate(todo)}
    new_group_place: dict[int, np.ndarray] = {}
    for g, members in enumerate(members_by_group):
        if has_placed[g] or not any(int(m) in todo_index for m in members.tolist()):
            continue
        to_placed = adjacency[g] * has_placed
        if to_placed.sum() > 0:
            center = centroids[int(np.argmax(to_placed))]
            plane_r = float(np.hypot(center[0], center[1]))
            rng = node_rng("anchor:" + group_keys[g])
            new_group_place[g] = center + np.array(
                [
                    rng.normal(0.0, SIGMA_PERP),
                    rng.normal(0.0, SIGMA_PERP),
                    rng.normal(0.0, sigma_z(plane_r)),
                ]
            )
        else:
            arm, s = hash_arm_slot("iso:" + group_keys[g])
            new_group_place[g] = arm_place(
                node_rng("iso:" + group_keys[g]), s * THETA_MAX, arm
            )

    # рёбра инцидентные todo — для барицентров §4.3
    neighbors: dict[int, list[tuple[int, float]]] = {int(i): [] for i in todo}
    for u, v, w in zip(
        inp.edges[:, 0].tolist(), inp.edges[:, 1].tolist(), inp.edge_weights.tolist()
    ):
        if v in neighbors:
            neighbors[v].append((u, w))
        if u in neighbors:
            neighbors[u].append((v, w))

    for pos, node in enumerate(todo):
        g = int(group_of[node])
        rng = node_rng(inp.node_ids[node])
        if g >= 0:
            if has_placed[g]:  # (1) центроид размещённых членов группы
                sigma = cluster_sigma(int(sizes[g]))
                coords[pos] = centroids[g] + rng.normal(0.0, sigma, size=3) * _MEMBER_SCALE
                region_of[pos] = _ARM
            elif g in new_group_place:  # (2) место новой группы уже посчитано
                sigma = cluster_sigma(int(sizes[g]))
                coords[pos] = new_group_place[g] + rng.normal(0.0, sigma, size=3) * _MEMBER_SCALE
                region_of[pos] = _ARM
            continue
        seated = [
            (nbr, w)
            for nbr, w in neighbors.get(int(node), ())
            if not np.isnan(placed_coords[nbr, 0])
        ]
        if seated:  # (3) взвешенный барицентр топ-3 соседей
            seated.sort(key=lambda nw: -nw[1])
            top = seated[:3]
            total = sum(w for _, w in top)
            barycenter = sum(placed_coords[nbr] * w for nbr, w in top) / total
            coords[pos] = barycenter + rng.normal(0.0, NEIGHBOR_JITTER, size=3) * _NEIGHBOR_SCALE
            region_of[pos] = _SATELLITE
        else:  # (4) полный одиночка — гало
            coords[pos] = halo_point(rng)
            region_of[pos] = _HALO

    bad = ~np.isfinite(coords)
    if bad.any():
        coords[bad] = 0.0
    merged = placed_coords.copy()
    merged[todo] = coords
    report = GalacticReport(
        mode="incremental",
        placed=len(todo),
        regions=_region_counts(region_of),
        edge_median_len=_median_edge_len(merged, inp),
    )
    return todo, coords, report
