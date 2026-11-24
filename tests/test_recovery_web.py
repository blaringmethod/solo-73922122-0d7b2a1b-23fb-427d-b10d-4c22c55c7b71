# -*- coding: utf-8 -*-
"""查档恢复端到端测试：勾选生成清单、换账续办、一次录齐、失败保留、牵制环。"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_tmpdir = tempfile.mkdtemp(prefix="audit-recovery-e2e-")
os.environ["AUDIT_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["AUDIT_SECRET_KEY"] = "test-secret"

from app import create_app  # noqa: E402
from app import db  # noqa: E402

app = create_app()
app.config["TESTING"] = True
client = app.test_client()

# 演示账套：F1->F2 两笔平行转料 + F1->F2->F3->F1 环 + F3->F4->F1 链
REC_CSV = (
    "record_type,furnace,ref,primary_kg,recycled_kg,weight_kg\n"
    "furnace,F1,,100,100,\n"
    "furnace,F2,,0,60,\n"
    "furnace,F3,,55,50,\n"
    "furnace,F4,,20,50,\n"
    "batch,F1,B1,,,165\n"
    "batch,F2,B2,,,100\n"
    "batch,F3,B3,,,100\n"
    "batch,F4,B4,,,70\n"
    "transfer,F1,F2,,,50\n"   # T1
    "transfer,F1,F2,,,30\n"   # T2
    "transfer,F2,F3,,,40\n"   # T3
    "transfer,F3,F1,,,20\n"   # T4
    "transfer,F3,F4,,,25\n"   # T5
    "transfer,F4,F1,,,25\n"   # T6
)

OTHER_CSV = (
    "record_type,furnace,ref,primary_kg,recycled_kg,weight_kg\n"
    "furnace,G1,,0,100,\n"
    "batch,G1,BB1,,,100\n"
)

TRUE = {}  # tid -> 真值，上传后填充
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))


def upload(csv_text, replace=False, filename="ledger.csv"):
    data = {"csv_file": (io.BytesIO(csv_text.encode("utf-8")), filename)}
    if replace:
        data["replace"] = "1"
    return client.post("/upload", data=data, content_type="multipart/form-data",
                       follow_redirects=True)


def sql(query, args=()):
    conn = db.get_conn()
    try:
        return [dict(r) for r in conn.execute(query, args).fetchall()]
    finally:
        conn.close()


def plan_form(tids, minutes, na=()):
    """构造勾选表单：minutes: {tid: 分钟}，na: 无法调阅的 tid 集合。"""
    data = {"ledger_id": str(REC_LID)}
    for tid in tids:
        data.setdefault("sel", []).append(str(tid))
    # Flask test client 需要 list 型值来传多值字段
    data["sel"] = [str(t) for t in tids]
    for tid in tids:
        if tid in na:
            data[f"na_{tid}"] = "1"
        else:
            data[f"min_{tid}"] = str(minutes[tid])
    return data


# --- 1. 上传演示账套 ---
r = upload(REC_CSV)
check("upload_rec_ok", r.status_code == 200)
REC_LID = sql("SELECT id FROM ledgers WHERE is_current=1")[0]["id"]
rows = sql("SELECT id, src_furnace, dst_furnace, weight_kg FROM transfers "
           "WHERE ledger_id=? ORDER BY id", (REC_LID,))
check("six_transfers", len(rows) == 6)
for row, w in zip(rows, [50, 30, 40, 20, 25, 25]):
    TRUE[row["id"]] = w
T = [row["id"] for row in rows]  # T[0..5] 对应 T1..T6

r = client.get(f"/?ledger={REC_LID}")
check("page_has_recovery_section", "查档".encode() in r.data and 'id="recovery"'.encode() in r.data)

# --- 2. 未勾选直接提交被拒，且不产生清单 ---
r = client.post("/recovery/plan", data={"ledger_id": str(REC_LID)},
                follow_redirects=True)
check("plan_requires_selection", "至少勾选一笔".encode() in r.data)
check("no_list_yet", sql("SELECT COUNT(*) c FROM recovery_lists")[0]["c"] == 0)

# --- 3. 分钟数非法（0/空/非整数）被拒 ---
bad = plan_form(T, {T[0]: 0, T[1]: 5, T[2]: 8, T[3]: 6, T[4]: 12, T[5]: 9})
r = client.post("/recovery/plan", data=bad, follow_redirects=True)
check("plan_zero_minutes_rejected", "正整数分钟".encode() in r.data)
check("list_not_saved_on_bad_input",
      sql("SELECT COUNT(*) c FROM recovery_lists")[0]["c"] == 0)

# --- 4. 全部勾选 + 合法分钟 -> 最少耗时清单（T2,T3,T4 共 19 分钟） ---
mins = {T[0]: 10, T[1]: 5, T[2]: 8, T[3]: 6, T[4]: 12, T[5]: 9}
r = client.post("/recovery/plan", data=plan_form(T, mins), follow_redirects=True)
check("plan_saved", "总耗时 19 分钟".encode() in r.data, r.status_code)
lst = sql("SELECT * FROM recovery_lists WHERE ledger_id=?", (REC_LID,))
check("list_persisted", len(lst) == 1 and lst[0]["feasible"] == 1
      and lst[0]["total_minutes"] == 19, str(lst))
items = sql("SELECT * FROM recovery_items WHERE ledger_id=? ORDER BY transfer_id",
            (REC_LID,))
retrieve_ids = sorted(i["transfer_id"] for i in items if i["retrieve"])
check("retrieve_set_min", retrieve_ids == sorted([T[1], T[2], T[3]]), str(retrieve_ids))
check("derive_set", sorted(i["transfer_id"] for i in items if not i["retrieve"])
      == sorted([T[0], T[4], T[5]]))

# --- 5. 换账后回原账套续办：清单仍在 ---
r = upload(OTHER_CSV, replace=True)
check("switch_ledger_ok", r.status_code == 200)
other_lid = sql("SELECT id FROM ledgers WHERE is_current=1")[0]["id"]
check("switched", other_lid != REC_LID)
r = client.get(f"/?ledger={REC_LID}")
check("resume_after_switch",
      "调档清单".encode() in r.data and "19 分钟".encode() in r.data)
check("list_untouched_by_switch",
      sql("SELECT COUNT(*) c FROM recovery_items WHERE ledger_id=?",
          (REC_LID,))[0]["c"] == 6)

# --- 6. 录入缺项/非整数/负数均被拒，清单保留 ---
entry = {"ledger_id": str(REC_LID), f"act_{T[1]}": "30", f"act_{T[2]}": "40"}
r = client.post("/recovery/entry", data=entry, follow_redirects=True)  # 缺 T4
check("entry_incomplete_rejected", "一次录齐".encode() in r.data)
entry[f"act_{T[3]}"] = "abc"
r = client.post("/recovery/entry", data=entry, follow_redirects=True)
check("entry_non_int_rejected", "非负整数".encode() in r.data)
entry[f"act_{T[3]}"] = "-1"
r = client.post("/recovery/entry", data=entry, follow_redirects=True)
check("entry_negative_rejected", "非负整数".encode() in r.data)
check("list_kept_after_bad_entry",
      sql("SELECT COUNT(*) c FROM recovery_lists WHERE ledger_id=?",
          (REC_LID,))[0]["c"] == 1)
check("no_values_after_bad_entry",
      sql("SELECT COUNT(*) c FROM recovery_values WHERE ledger_id=?",
          (REC_LID,))[0]["c"] == 0)

# --- 7. 实称值导致推算为负 -> 整体回滚，清单保留 ---
entry[f"act_{T[1]}"] = "500"  # T2 虚高 -> 推算 T1 为负
entry[f"act_{T[3]}"] = "20"
r = client.post("/recovery/entry", data=entry, follow_redirects=True)
check("entry_negative_derived_rejected", "负值".encode() in r.data)
check("no_values_after_negative",
      sql("SELECT COUNT(*) c FROM recovery_values WHERE ledger_id=?",
          (REC_LID,))[0]["c"] == 0)

# --- 8. 一次录齐真值 -> 推算正确并另存，标明实查/推算 ---
entry[f"act_{T[1]}"] = str(TRUE[T[1]])  # 30
entry[f"act_{T[2]}"] = str(TRUE[T[2]])  # 40
entry[f"act_{T[3]}"] = str(TRUE[T[3]])  # 20
r = client.post("/recovery/entry", data=entry, follow_redirects=True)
check("entry_saved", "恢复完成".encode() in r.data and "实查 3 笔、推算 3 笔".encode() in r.data)
vals = {v["transfer_id"]: v for v in sql(
    "SELECT * FROM recovery_values WHERE ledger_id=?", (REC_LID,))}
check("six_values_saved", len(vals) == 6)
check("measured_marked",
      all(vals[t]["source"] == "实查" and vals[t]["recovered_kg"] == TRUE[t]
          for t in (T[1], T[2], T[3])))
check("derived_correct",
      vals[T[0]]["recovered_kg"] == 50 and vals[T[0]]["source"] == "推算"
      and vals[T[4]]["recovered_kg"] == 25 and vals[T[4]]["source"] == "推算"
      and vals[T[5]]["recovered_kg"] == 25 and vals[T[5]]["source"] == "推算",
      str({k: v["recovered_kg"] for k, v in vals.items()}))
r = client.get(f"/?ledger={REC_LID}")
check("page_shows_values", "实查".encode() in r.data and "推算".encode() in r.data)

# --- 9. 已有恢复结果时录入失败 -> 旧结果保留 ---
bad_entry = {"ledger_id": str(REC_LID), f"act_{T[1]}": "500",
             f"act_{T[2]}": "40", f"act_{T[3]}": "20"}
r = client.post("/recovery/entry", data=bad_entry, follow_redirects=True)
check("second_entry_fails", "负值".encode() in r.data)
vals2 = sql("SELECT * FROM recovery_values WHERE ledger_id=?", (REC_LID,))
check("old_values_kept_on_failure", len(vals2) == 6)
check("list_kept_on_failure",
      sql("SELECT total_minutes t FROM recovery_lists WHERE ledger_id=?",
          (REC_LID,))[0]["t"] == 19)

# --- 10. 重新勾选（含无法调阅环）-> 指出互相牵制，旧恢复结果作废 ---
r = client.post("/recovery/plan",
                data=plan_form([T[0], T[2], T[3]], {}, na={T[0], T[2], T[3]}),
                follow_redirects=True)
check("cycle_reported", "互相牵制".encode() in r.data or "牵制".encode() in r.data)
lst = sql("SELECT * FROM recovery_lists WHERE ledger_id=?", (REC_LID,))[0]
check("list_infeasible", lst["feasible"] == 0 and lst["cycle_note"])
check("values_invalidated_on_replan",
      sql("SELECT COUNT(*) c FROM recovery_values WHERE ledger_id=?",
          (REC_LID,))[0]["c"] == 0)
# 牵制环上的三笔都在说明里
note = lst["cycle_note"]
check("cycle_members", all(f"#{t}" in note for t in (T[0], T[2], T[3])), note)

# --- 11. 无法调阅但不成环 -> 全部可推算，无需调档 ---
r = client.post("/recovery/plan",
                data=plan_form([T[4], T[5]], {}, na={T[4], T[5]}),
                follow_redirects=True)
check("all_derive_plan", "需调阅 0 张".encode() in r.data)
r = client.post("/recovery/entry", data={"ledger_id": str(REC_LID)},
                follow_redirects=True)
check("all_derive_saved", "实查 0 笔、推算 2 笔".encode() in r.data)
vals = {v["transfer_id"]: v for v in sql(
    "SELECT * FROM recovery_values WHERE ledger_id=?", (REC_LID,))}
check("all_derive_values",
      vals[T[4]]["recovered_kg"] == 25 and vals[T[5]]["recovered_kg"] == 25
      and all(v["source"] == "推算" for v in vals.values()))

# --- 12. 清空清单 ---
r = client.post("/recovery/clear", data={"ledger_id": str(REC_LID)},
                follow_redirects=True)
check("clear_ok", "已清空".encode() in r.data)
check("cleared",
      sql("SELECT COUNT(*) c FROM recovery_lists WHERE ledger_id=?",
          (REC_LID,))[0]["c"] == 0
      and sql("SELECT COUNT(*) c FROM recovery_items WHERE ledger_id=?",
              (REC_LID,))[0]["c"] == 0
      and sql("SELECT COUNT(*) c FROM recovery_values WHERE ledger_id=?",
              (REC_LID,))[0]["c"] == 0)

# --- 13. 审计日志覆盖查档动作（含失败） ---
logs = sql("SELECT action, success FROM audit_log")
actions = {l["action"] for l in logs}
check("recovery_actions_logged",
      {"recovery_plan", "recovery_entry", "recovery_clear"}.issubset(actions),
      str(actions))
check("recovery_failures_logged",
      any(l["action"].startswith("recovery") and l["success"] == 0 for l in logs))

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
