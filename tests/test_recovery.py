# -*- coding: utf-8 -*-
"""查档恢复求解器测试：最少耗时调档清单、牵制环、守恒推算与负值/不平衡拦截。"""
import itertools
import os
import random
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.recovery import (  # noqa: E402
    RecoveryError, UnknownTransfer, check_all_balanced, derive_weights,
    plan_retrieval,
)

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))


# --- 1. 链式无环：无需调档，全部可推算 ---
plan = plan_retrieval([
    UnknownTransfer(1, "F1", "F2", 10),
    UnknownTransfer(2, "F2", "F3", 20),
])
check("chain_no_retrieval", plan.ok and plan.retrieve_ids == [] and plan.total_minutes == 0)

# --- 2. 三角环：只调档最便宜的一张 ---
plan = plan_retrieval([
    UnknownTransfer(1, "F1", "F2", 10),
    UnknownTransfer(2, "F2", "F3", 8),
    UnknownTransfer(3, "F3", "F1", 6),
])
check("triangle_cheapest", plan.ok and plan.retrieve_ids == [3] and plan.total_minutes == 6)

# --- 3. 同一对炉平行转料：逐笔核算，便宜的那笔去调档 ---
plan = plan_retrieval([
    UnknownTransfer(1, "F1", "F2", 10),
    UnknownTransfer(2, "F1", "F2", 4),
])
check("parallel_cheaper_retrieved",
      plan.ok and plan.retrieve_ids == [2] and plan.keep_ids == [1])

# --- 4. 无法调阅的边成环：推不全并指出互相牵制的一组 ---
plan = plan_retrieval([
    UnknownTransfer(1, "F1", "F2", None),
    UnknownTransfer(2, "F2", "F3", None),
    UnknownTransfer(3, "F3", "F1", None),
    UnknownTransfer(4, "F1", "F3", 1),
])
check("unretrievable_cycle_infeasible",
      not plan.ok and set(plan.cycle_ids) == {1, 2, 3} and "牵制" in plan.cycle_note)

# --- 5. 无法调阅的边永不被列入调档 ---
plan = plan_retrieval([
    UnknownTransfer(1, "F1", "F2", None),
    UnknownTransfer(2, "F1", "F2", 7),
])
check("unretrievable_never_retrieved",
      plan.ok and plan.retrieve_ids == [2] and plan.keep_ids == [1])

# --- 6. 随机小图暴力对拍：调档总耗时确为最少 ---
def brute_force_min(unknowns):
    tids = [u.tid for u in unknowns]
    best = None
    for r in range(len(tids) + 1):
        for combo in itertools.combinations(range(len(tids)), r):
            picked = [unknowns[i] for i in combo]
            if any(u.minutes is None for u in picked):
                continue  # 无法调阅的不能调档
            rest = [u for i, u in enumerate(unknowns) if i not in combo]
            # rest 必须无环
            parent = {}
            def find(x):
                parent.setdefault(x, x)
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x
            acyclic = True
            for u in rest:
                if find(u.src) == find(u.dst):
                    acyclic = False
                    break
                parent[find(u.src)] = find(u.dst)
            if not acyclic:
                continue
            cost = sum(u.minutes for u in picked)
            if best is None or cost < best:
                best = cost
    return best

rng = random.Random(20261124)
ok_all = True
for trial in range(300):
    nodes = [f"F{i}" for i in range(rng.randint(2, 5))]
    m = rng.randint(1, 9)
    unknowns = []
    for tid in range(1, m + 1):
        a, b = rng.sample(nodes, 2)
        minutes = None if rng.random() < 0.25 else rng.randint(1, 20)
        unknowns.append(UnknownTransfer(tid, a, b, minutes))
    plan = plan_retrieval(unknowns)
    bf = brute_force_min(unknowns)
    if bf is None:
        if plan.ok:
            ok_all = False
            print("  应不可行却可行:", unknowns)
            break
    else:
        if not plan.ok or plan.total_minutes != bf:
            ok_all = False
            print(f"  不一致: plan={plan.total_minutes if plan.ok else 'infeasible'} brute={bf}",
                  unknowns)
            break
check("random_bruteforce_min_cost", ok_all)

# --- 7. 推算：链式账本唯一解与真值一致 ---
# F1: 进 200(100+100) + T3 20 = 出 B1 165 + T1 55；F2: 进 60+T1 55 = 出 B2 95 + T2 20
# F3: 进 50+T2 20 = 出 B3 50 + T3 20
f_in = [("F1", 100, 100), ("F2", 0, 60), ("F3", 0, 50)]
b_in = [("F1", 165), ("F2", 95), ("F3", 50)]
true = {1: 55, 2: 20, 3: 20}
known = [("F2", "F3", true[2])]          # T2 已实查
unknown = [(1, "F1", "F2"), (3, "F3", "F1")]
got = derive_weights(f_in, b_in, known, unknown)
check("derive_chain_matches_truth", got == {1: 55, 3: 20}, str(got))

# --- 8. 推算：实称值不一致（未知图连通分量内各炉无法找平）被拒 ---
# 把 B1 改成 166 后全账不再守恒，分量 rhs 之和 ≠ 0
b_bad = [("F1", 166), ("F2", 95), ("F3", 50)]
try:
    derive_weights(f_in, b_bad, [("F2", "F3", 20)], unknown)
    check("derive_inconsistent_rejected", False)
except RecoveryError as exc:
    check("derive_inconsistent_rejected", "找平" in str(exc), str(exc))

# --- 9. 推算：方程一致但解出负值被拒 ---
# F1: 进 10、批出 100、未知出 x -> x = -90；F2: 进 100、批出 10、未知入 x -> x = -90
f2 = [("F1", 0, 10), ("F2", 0, 100)]
b2 = [("F1", 100), ("F2", 10)]
try:
    derive_weights(f2, b2, [], [(1, "F1", "F2")])
    check("derive_negative_rejected", False)
except RecoveryError as exc:
    check("derive_negative_rejected", "负值" in str(exc), str(exc))

# --- 10. 推算：平行边森林（多重图）也能解 ---
# F1<->F2 两条平行未知边不行（成环），但一条未知 + 一条已知可解
f3 = [("F1", 0, 0), ("F2", 0, 0)]
b3 = []
got = derive_weights(f3, b3, [("F1", "F2", 30)], [(1, "F2", "F1")])
check("derive_multigraph", got == {1: 30}, str(got))

# --- 11. 全部已知后复核平衡 ---
check("balance_ok",
      check_all_balanced(f_in, b_in,
                         [("F1", "F2", 55), ("F2", "F3", 20), ("F3", "F1", 20)]) is None)
check("balance_bad",
      check_all_balanced(f_in, b_in, [("F1", "F2", 56)]) is not None)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
