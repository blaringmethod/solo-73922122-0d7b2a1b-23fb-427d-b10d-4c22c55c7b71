# -*- coding: utf-8 -*-
"""回炉转料暂用估值的查档恢复求解。

把每座炉看作节点、每笔勾选待恢复的转料看作一条有向边（同一对炉之间
逐笔核算，允许平行边）。勾选笔的原估值视为缺失，未知量即这些边的重量；
每炉“总进料 = 总出料”给出一个线性方程，全体系数矩阵正是图的关联矩阵，
因此未知边集能被唯一推算 ⟺ 这些边在（无向）图上不含环，即构成森林。

- plan_retrieval：在可调阅的边里挑一组总耗时最小的磅单去调档，使剩下的
  未知边构成森林。做法：无法调阅的边权视为 +∞（必须留在未知集），可调阅
  边按分钟数从大到小做 Kruskal 最大权森林；森林之外的边即最少耗时的调档
  清单。若无法调阅的边自身已经成环，则全部找回也推不全，返回环上那组
  互相牵制的转料。
- derive_weights：拿到调档实称值后，对未知边森林做叶剥离，唯一推出其余
  重量；推出负值或残余节点不守恒时抛 RecoveryError。
- check_all_balanced：全部重量就绪后复核每炉 总进料 = 总出料。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


class RecoveryError(Exception):
    """实称录入不一致或推算结果不合法（负值/不平衡/数据异常）。"""


@dataclass
class UnknownTransfer:
    """一笔勾选待恢复的转料。minutes 为调阅原磅单的正整数分钟数，None 表示无法调阅。"""
    tid: int
    src: str
    dst: str
    minutes: Optional[int]


@dataclass
class PlanResult:
    ok: bool
    retrieve_ids: List[int] = field(default_factory=list)  # 需调档实查的转料记录#
    keep_ids: List[int] = field(default_factory=list)      # 留待推算的转料记录#
    total_minutes: int = 0                                 # 调档总耗时（分钟）
    cycle_ids: List[int] = field(default_factory=list)     # 互相牵制的转料环
    cycle_note: str = ""


class _DSU:
    """并查集（路径压缩）。"""

    def __init__(self, items):
        self.parent = {x: x for x in items}

    def find(self, x):
        p = self.parent
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _path_edges(adj, start, goal) -> List[UnknownTransfer]:
    """在已保留边构成的森林上找 start→goal 的简单路径，返回途经的边。"""
    prev = {start: None}
    q = deque([start])
    while q:
        u = q.popleft()
        if u == goal:
            break
        for v, e in adj[u]:
            if v not in prev:
                prev[v] = (u, e)
                q.append(v)
    edges: List[UnknownTransfer] = []
    node = goal
    while prev.get(node):
        u, e = prev[node]
        edges.append(e)
        node = u
    edges.reverse()
    return edges


def plan_retrieval(unknowns: Sequence[UnknownTransfer]) -> PlanResult:
    """生成总耗时最少的调档清单。

    unknowns：全部勾选待恢复的转料（原估值视为缺失）。
    返回的 retrieve_ids 即需调档实查的最少耗时磅单组；其余留待推算。
    若无法调阅的转料自身成环，则 ok=False 并给出互相牵制的转料环。
    """
    nodes = set()
    for e in unknowns:
        nodes.add(e.src)
        nodes.add(e.dst)
    dsu = _DSU(nodes)
    adj: Dict[str, List[Tuple[str, UnknownTransfer]]] = {n: [] for n in nodes}
    kept: List[UnknownTransfer] = []
    retrieve: List[UnknownTransfer] = []

    def keep(e: UnknownTransfer) -> None:
        dsu.union(e.src, e.dst)
        adj[e.src].append((e.dst, e))
        adj[e.dst].append((e.src, e))
        kept.append(e)

    # 无法调阅的边必须留在未知集（权 +∞），先入林；若成环则无法推全
    for e in sorted((x for x in unknowns if x.minutes is None), key=lambda x: x.tid):
        if dsu.find(e.src) == dsu.find(e.dst):
            cyc = _path_edges(adj, e.src, e.dst) + [e]
            note = "、".join(f"转料#{c.tid}（{c.src}→{c.dst}）" for c in cyc)
            return PlanResult(
                ok=False,
                cycle_ids=[c.tid for c in cyc],
                cycle_note=(
                    note + "：以上转料均无法调阅且首尾相接成环，"
                    "守恒方程相互牵制，无法唯一推算。"
                ),
            )
        keep(e)

    # 可调阅边按分钟从大到小入林：耗时长的留下推算，耗时短的才去调档，
    # 森林之外即总耗时最小的调档集合（最大权森林的补集）。
    for e in sorted((x for x in unknowns if x.minutes is not None),
                    key=lambda x: (-x.minutes, x.tid)):
        if dsu.find(e.src) == dsu.find(e.dst):
            retrieve.append(e)
        else:
            keep(e)

    retrieve.sort(key=lambda x: x.tid)
    return PlanResult(
        ok=True,
        retrieve_ids=[e.tid for e in retrieve],
        keep_ids=[e.tid for e in sorted(kept, key=lambda x: x.tid)],
        total_minutes=sum(e.minutes for e in retrieve),
    )


def derive_weights(
    furnaces: Sequence[Tuple[str, int, int]],           # (炉号, 原生kg, 再生kg)
    batches: Sequence[Tuple[str, int]],                 # (归属炉, 实称kg)
    known_transfers: Sequence[Tuple[str, str, int]],    # (源炉, 目标炉, kg) 未勾选+已实查
    unknown_transfers: Sequence[Tuple[int, str, str]],  # (记录#, 源炉, 目标炉) 待推算
) -> Dict[int, int]:
    """用每炉进出守恒唯一推出待恢复转料的重量（森林叶剥离）。

    返回 {记录#: 非负整数kg}；实称值不一致（残余节点不守恒）或推算出现
    负值时抛 RecoveryError。
    """
    # 方程：Σ入未知 − Σ出未知 = rhs；rhs = 已知出 − 已知进
    rhs: Dict[str, int] = {}
    for code, primary, recycled in furnaces:
        rhs[code] = rhs.get(code, 0) - (primary + recycled)
    for code, w in batches:
        rhs[code] = rhs.get(code, 0) + w
    for src, dst, w in known_transfers:
        rhs[src] = rhs.get(src, 0) + w
        rhs[dst] = rhs.get(dst, 0) - w

    adj: Dict[str, List[List]] = {c: [] for c in rhs}
    seen_tids = set()
    for tid, src, dst in unknown_transfers:
        if tid in seen_tids:
            raise RecoveryError(f"转料#{tid} 重复列入待推算，内部数据异常。")
        seen_tids.add(tid)
        if src == dst:
            raise RecoveryError(f"转料#{tid} 源炉与目标炉相同，无法核算。")
        adj.setdefault(src, []).append([tid, dst, -1])  # 对 src 是出边
        adj.setdefault(dst, []).append([tid, src, +1])  # 对 dst 是入边

    values: Dict[int, int] = {}
    remaining = len(unknown_transfers)
    queue = deque([n for n, es in adj.items() if len(es) == 1])
    done = set()
    while queue:
        n = queue.popleft()
        if n in done or not adj[n]:
            continue
        tid, other, sign = adj[n].pop()
        # 节点 n 只剩这一条未知边：sign·x = rhs[n]，且 sign = ±1
        x = sign * rhs[n]
        values[tid] = x
        remaining -= 1
        rhs[n] = 0  # n 的守恒方程已由该边满足
        # 该边对另一端 other 的贡献为 −sign·x，移项得 rhs[other] += sign·x
        rhs[other] += sign * x
        lst = adj[other]
        for i, entry in enumerate(lst):
            if entry[0] == tid:
                lst.pop(i)
                break
        done.add(n)
        if len(lst) == 1:
            queue.append(other)

    if remaining:
        raise RecoveryError("待推算转料含环，无法唯一求解（内部数据异常）。")
    unbalanced = sorted(c for c, r in rhs.items() if r != 0)
    if unbalanced:
        raise RecoveryError(
            "录入的实称值与守恒方程不一致：炉 " + "、".join(unbalanced)
            + " 的进出无法找平，请核对磅单数字。"
        )
    neg = sorted((t, v) for t, v in values.items() if v < 0)
    if neg:
        parts = "、".join(f"转料#{t}（{v} kg）" for t, v in neg)
        raise RecoveryError("推算出现负值：" + parts + "，请核对已录入的实称值。")
    return values


def check_all_balanced(
    furnaces: Sequence[Tuple[str, int, int]],
    batches: Sequence[Tuple[str, int]],
    transfers: Sequence[Tuple[str, str, int]],
) -> Optional[str]:
    """全部重量就绪后复核每炉 总进料 = 总出料；不平衡返回中文说明，否则 None。"""
    bal: Dict[str, List[int]] = {}
    for code, primary, recycled in furnaces:
        bal.setdefault(code, [0, 0])
        bal[code][0] += primary + recycled
    for code, w in batches:
        bal.setdefault(code, [0, 0])[1] += w
    for src, dst, w in transfers:
        bal.setdefault(src, [0, 0])[1] += w
        bal.setdefault(dst, [0, 0])[0] += w
    bad = [f"炉 {c}（总进 {v[0]} kg ≠ 总出 {v[1]} kg）"
           for c, v in sorted(bal.items()) if v[0] != v[1]]
    if bad:
        return "恢复后各炉不平衡：" + "；".join(bad) + "。"
    return None
