# -*- coding: utf-8 -*-
"""铝厂余料/再生料申报审核页 —— Flask 应用。

写操作统一在单事务内完成：先在内存中用求解器试算，成功才 COMMIT；
任何失败均 ROLLBACK，原账、签单与既有核定结果保持不变。
"""
from __future__ import annotations

import os
from typing import Optional, Tuple

from flask import (
    Flask, flash, redirect, render_template, request, url_for,
)

from . import csvutil, db
from .recovery import (
    RecoveryError, UnknownTransfer, check_all_balanced, derive_weights,
    plan_retrieval,
)
from .solver import (
    CertRequirement, FurnaceBatchLink, FurnaceInput, SolveError, TransferInput,
    solve_allocation,
)


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
    app.config["SECRET_KEY"] = os.environ.get("AUDIT_SECRET_KEY", "dev-audit-secret")

    conn = db.get_conn()
    db.init_db(conn)
    conn.close()

    # ------------------------------------------------------------------
    def _build_solver_inputs(conn, ledger_id):
        furnaces, batches, transfers = db.load_ledger(conn, ledger_id)
        f_inputs = [
            FurnaceInput(r["code"], r["primary_kg"], r["recycled_kg"])
            for r in furnaces
        ]
        links = [
            FurnaceBatchLink(r["furnace_code"], r["code"], r["weight_kg"])
            for r in batches
        ]
        t_inputs = [
            TransferInput(r["id"], r["src_furnace"], r["dst_furnace"], r["weight_kg"])
            for r in transfers
        ]
        reqs = [
            CertRequirement(r["batch_code"], r["min_recycled_kg"])
            for r in db.active_certificates(conn)
        ]
        return f_inputs, links, t_inputs, reqs

    def _run_and_persist(conn, ledger_id: int) -> Tuple[bool, Optional[str]]:
        """在当前事务内求解并覆盖保存该账套核定结果。"""
        f_inputs, links, t_inputs, reqs = _build_solver_inputs(conn, ledger_id)
        result = solve_allocation(f_inputs, links, t_inputs, reqs)
        if result.ok:
            db.save_solution(
                conn, ledger_id, True, None,
                batch_values=result.batch_recycled.items(),
                transfer_values=result.transfer_recycled.items(),
            )
            return True, None
        db.save_solution(conn, ledger_id, False, result.reason)
        return False, result.reason

    def _log_failure(action: str, detail: str) -> None:
        """业务事务回滚后，用独立事务补记一条失败审计日志（不影响回滚结果）。"""
        log_conn = db.get_conn()
        try:
            with db.transaction(log_conn):
                db.log_event(log_conn, action, detail, False)
        except Exception:  # pragma: no cover 日志失败不应掩盖原始错误
            pass
        finally:
            log_conn.close()

    # ------------------------------------------------------------------
    @app.route("/")
    def index():
        conn = db.get_conn()
        try:
            ledger_id = db.current_ledger_id(conn)
            view_ledger_id = request.args.get("ledger", type=int) or ledger_id

            ledgers = conn.execute(
                "SELECT * FROM ledgers ORDER BY id DESC LIMIT 20"
            ).fetchall()
            furnaces = batches = transfers = []
            sol_row = None
            sol_batches = sol_transfers = []
            ledger_row = None
            rec_list = None
            rec_items = []
            rec_values = []
            if view_ledger_id:
                ledger_row = conn.execute(
                    "SELECT * FROM ledgers WHERE id = ?", (view_ledger_id,)
                ).fetchone()
                if ledger_row:
                    furnaces, batches, transfers = db.load_ledger(conn, view_ledger_id)
                    sol_row = db.get_solution(conn, view_ledger_id)
                    if sol_row:
                        sol_batches, sol_transfers = db.solution_details(
                            conn, sol_row["id"]
                        )
                    rec_list, rec_items = db.load_recovery_plan(conn, view_ledger_id)
                    rec_values = db.load_recovery_values(conn, view_ledger_id)
            certs = conn.execute(
                "SELECT * FROM certificates ORDER BY active DESC, signed_at DESC, id DESC"
            ).fetchall()
            logs = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT 15"
            ).fetchall()

            cert_map = {c["batch_code"]: c for c in certs if c["active"]}
            sol_batch_map = {r["batch_code"]: r["recycled_kg"] for r in sol_batches}
            sol_transfer_map = {r["transfer_id"]: r["recycled_kg"] for r in sol_transfers}

            # 查档恢复：清单明细、调档/推算分组与已存恢复结果
            rec_item_map = {i["transfer_id"]: i for i in rec_items}
            rec_retrieve = [i for i in rec_items if i["retrieve"]]
            rec_derive = [i for i in rec_items if not i["retrieve"]]
            transfer_map = {t["id"]: t for t in transfers}

            # 每炉物料平衡（账套原值，供页面核对“总进料=总出料”）
            balance = {}
            if ledger_row:
                for f in furnaces:
                    balance[f["code"]] = {
                        "primary": f["primary_kg"], "recycled": f["recycled_kg"],
                        "t_in": 0, "t_out": 0, "ship": 0,
                    }
                for t in transfers:
                    balance[t["src_furnace"]]["t_out"] += t["weight_kg"]
                    balance[t["dst_furnace"]]["t_in"] += t["weight_kg"]
                for b in batches:
                    balance[b["furnace_code"]]["ship"] += b["weight_kg"]
                for row in balance.values():
                    row["in_total"] = row["primary"] + row["recycled"] + row["t_in"]
                    row["out_total"] = row["ship"] + row["t_out"]

            return render_template(
                "index.html",
                ledgers=ledgers,
                ledger_row=ledger_row,
                current_ledger_id=ledger_id,
                view_ledger_id=view_ledger_id,
                furnaces=furnaces,
                batches=batches,
                transfers=transfers,
                balance=balance,
                certs=certs,
                cert_map=cert_map,
                sol_row=sol_row,
                sol_batch_map=sol_batch_map,
                sol_transfer_map=sol_transfer_map,
                rec_list=rec_list,
                rec_items=rec_items,
                rec_item_map=rec_item_map,
                rec_retrieve=rec_retrieve,
                rec_derive=rec_derive,
                rec_values=rec_values,
                transfer_map=transfer_map,
                logs=logs,
            )
        finally:
            conn.close()

    # ------------------------------------------------------------------
    @app.route("/upload", methods=["POST"])
    def upload():
        file = request.files.get("csv_file")
        replace = request.form.get("replace") == "1"
        if not file or not file.filename:
            flash("请选择要上传的 CSV 文件。", "error")
            return redirect(url_for("index"))
        try:
            content = file.read()
            parsed = csvutil.parse_ledger_csv(content)
        except csvutil.CsvError as exc:
            flash(f"CSV 校验未通过，未改动任何数据：{exc}", "error")
            _log_failure("upload", f"{file.filename if file else '?'} 校验失败：{exc}")
            return redirect(url_for("index"))

        conn = db.get_conn()
        try:
            with db.transaction(conn):
                if not replace and db.current_ledger_id(conn) is not None:
                    raise CsvConflict("当前已有生效账套。如确认换账，请勾选“替换当前账套”后重新上传。")

                cur = conn.execute(
                    "INSERT INTO ledgers(filename, imported_at, is_current, note) "
                    "VALUES (?, ?, 1, ?)",
                    (file.filename, db.utcnow_iso(),
                     f"{len(parsed.furnaces)}炉/{len(parsed.batches)}批/{len(parsed.transfers)}笔转料"),
                )
                new_id = cur.lastrowid
                conn.executemany(
                    "INSERT INTO furnaces(ledger_id, code, primary_kg, recycled_kg) "
                    "VALUES (?, ?, ?, ?)",
                    [(new_id, f["code"], f["primary_kg"], f["recycled_kg"])
                     for f in parsed.furnaces],
                )
                conn.executemany(
                    "INSERT INTO batches(ledger_id, furnace_code, code, weight_kg) "
                    "VALUES (?, ?, ?, ?)",
                    [(new_id, b["furnace"], b["code"], b["weight_kg"])
                     for b in parsed.batches],
                )
                conn.executemany(
                    "INSERT INTO transfers(ledger_id, src_furnace, dst_furnace, weight_kg) "
                    "VALUES (?, ?, ?, ?)",
                    [(new_id, t["src"], t["dst"], t["weight_kg"])
                     for t in parsed.transfers],
                )
                conn.execute("UPDATE ledgers SET is_current = 0 WHERE id <> ?", (new_id,))

                # 内存试算：无解则整笔回滚，原账/签单/核定结果原样保留
                f_inputs, links, t_inputs, reqs = _build_solver_inputs(conn, new_id)
                result = solve_allocation(f_inputs, links, t_inputs, reqs)
                if not result.ok:
                    raise CsvConflict(result.reason or "新账无可行解。")
                db.save_solution(
                    conn, new_id, True, None,
                    batch_values=result.batch_recycled.items(),
                    transfer_values=result.transfer_recycled.items(),
                )
                db.log_event(
                    conn, "upload",
                    f"导入 {file.filename}（账套#{new_id}）并核定成功", True,
                )
            flash(
                f"账套已导入并核定：{len(parsed.furnaces)} 座炉、"
                f"{len(parsed.batches)} 个出货批、{len(parsed.transfers)} 笔转料。",
                "ok",
            )
            return redirect(url_for("index", ledger=new_id))
        except CsvConflict as exc:
            flash(f"提交失败，原账、签单与核定结果均保留：{exc}", "error")
            _log_failure("upload", f"{file.filename} 被拒（回滚）：{exc}")
            return redirect(url_for("index"))
        except SolveError as exc:
            flash(f"提交失败，数据未改动：{exc}", "error")
            _log_failure("upload", f"{file.filename} 数据错误（回滚）：{exc}")
            return redirect(url_for("index"))
        finally:
            conn.close()

    # ------------------------------------------------------------------
    @app.route("/certificates/sign", methods=["POST"])
    def sign_cert():
        batch = (request.form.get("batch_code") or "").strip()
        raw_min = (request.form.get("min_recycled_kg") or "").strip()
        if not batch:
            flash("签发失败：出货批号不能为空。", "error")
            return redirect(url_for("index"))
        try:
            min_kg = int(raw_min)
            if min_kg <= 0:
                raise ValueError
        except ValueError:
            flash("签发失败：最低再生料重量必须是正整数公斤。", "error")
            return redirect(url_for("index"))

        conn = db.get_conn()
        try:
            with db.transaction(conn):
                cur_lid = db.current_ledger_id(conn)
                if cur_lid is None:
                    raise CsvConflict("尚未导入任何账套，无法签发。")
                existing = conn.execute(
                    "SELECT id FROM certificates WHERE batch_code = ? AND active = 1",
                    (batch,),
                ).fetchone()
                if existing:
                    raise CsvConflict(f"出货批 {batch} 已存在有效签单，每批最多一张；请先撤销原签单。")
                # 批号必须在当前账套内
                row = conn.execute(
                    "SELECT weight_kg FROM batches WHERE ledger_id = ? AND code = ?",
                    (cur_lid, batch),
                ).fetchone()
                if not row:
                    raise CsvConflict(f"当前账套不存在出货批 {batch}。")
                if min_kg > row["weight_kg"]:
                    raise CsvConflict(
                        f"批 {batch} 实称仅 {row['weight_kg']} kg，"
                        f"不能签发高于实称重量的最低再生料 {min_kg} kg。"
                    )
                conn.execute(
                    "INSERT INTO certificates(batch_code, min_recycled_kg, active, signed_at) "
                    "VALUES (?, ?, 1, ?)",
                    (batch, min_kg, db.utcnow_iso()),
                )
                ok, reason = _run_and_persist(conn, cur_lid)
                if not ok:
                    raise CsvConflict(reason or "加入该签单后无可行解。")
                db.log_event(conn, "sign", f"批 {batch} 签发最低再生料 {min_kg} kg", True)
            flash(f"已为批 {batch} 签发最低再生料 {min_kg} kg，并重新核定成功。", "ok")
            return redirect(url_for("index", ledger=cur_lid))
        except CsvConflict as exc:
            flash(f"签发失败，签单与核定结果保持原状：{exc}", "error")
            _log_failure("sign", f"批 {batch} 申请 {min_kg} kg 被拒（回滚）：{exc}")
            return redirect(url_for("index"))
        finally:
            conn.close()

    # ------------------------------------------------------------------
    @app.route("/certificates/revoke", methods=["POST"])
    def revoke_cert():
        cert_id = request.form.get("cert_id", type=int)
        conn = db.get_conn()
        try:
            with db.transaction(conn):
                cur_lid = db.current_ledger_id(conn)
                cert = conn.execute(
                    "SELECT * FROM certificates WHERE id = ? AND active = 1",
                    (cert_id,),
                ).fetchone()
                if not cert:
                    raise CsvConflict("签单不存在或已撤销。")
                conn.execute(
                    "UPDATE certificates SET active = 0, revoked_at = ? WHERE id = ?",
                    (db.utcnow_iso(), cert_id),
                )
                if cur_lid is not None:
                    ok, reason = _run_and_persist(conn, cur_lid)
                    if not ok:  # 撤销只会放松约束，理论上不会发生
                        raise CsvConflict(reason or "撤销后重核异常。")
                db.log_event(
                    conn, "revoke",
                    f"撤销批 {cert['batch_code']} 的签单（原下限 {cert['min_recycled_kg']} kg）",
                    True,
                )
            flash(f"已撤销批 {cert['batch_code']} 的有效签单，并重新核定。", "ok")
            return redirect(url_for("index", ledger=cur_lid))
        except CsvConflict as exc:
            flash(f"撤销失败，数据未改动：{exc}", "error")
            _log_failure("revoke", f"签单#{cert_id} 撤销被拒（回滚）：{exc}")
            return redirect(url_for("index"))
        finally:
            conn.close()

    # ------------------------------------------------------------------
    @app.route("/recheck", methods=["POST"])
    def recheck():
        conn = db.get_conn()
        try:
            with db.transaction(conn):
                cur_lid = db.current_ledger_id(conn)
                if cur_lid is None:
                    raise CsvConflict("尚未导入任何账套，无法重核。")
                ok, reason = _run_and_persist(conn, cur_lid)
                if not ok:
                    raise CsvConflict(reason or "重核无可行解。")
                db.log_event(conn, "recheck", f"账套#{cur_lid} 重核成功", True)
            flash("已按当前账套与全部有效签单重新核定，出货与回炉再生料重量已更新。", "ok")
            return redirect(url_for("index", ledger=cur_lid))
        except CsvConflict as exc:
            flash(f"重核失败，原有核定结果保留：{exc}", "error")
            _log_failure("recheck", f"账套重核被拒（回滚）：{exc}")
            return redirect(url_for("index"))
        finally:
            conn.close()

    # ------------------------------------------------------------------
    @app.route("/recovery/plan", methods=["POST"])
    def recovery_plan():
        """保存勾选并生成最少耗时的调档清单（按账套落库，换账可续办）。"""
        ledger_id = request.form.get("ledger_id", type=int)
        try:
            conn = db.get_conn()
            try:
                ledger = conn.execute(
                    "SELECT * FROM ledgers WHERE id = ?", (ledger_id,)
                ).fetchone()
                if not ledger:
                    raise CsvConflict("账套不存在，无法保存清单。")
                _, _, transfers = db.load_ledger(conn, ledger_id)
                by_id = {t["id"]: t for t in transfers}
                selected = set()
                for raw in request.form.getlist("sel"):
                    try:
                        tid = int(raw)
                    except (TypeError, ValueError):
                        continue
                    if tid in by_id:
                        selected.add(tid)
                if not selected:
                    raise CsvConflict("请至少勾选一笔待恢复的转料。")
                unknowns = []
                for tid in sorted(selected):
                    t = by_id[tid]
                    if request.form.get(f"na_{tid}") == "1":
                        minutes = None
                    else:
                        raw_min = (request.form.get(f"min_{tid}") or "").strip()
                        try:
                            minutes = int(raw_min)
                            if minutes <= 0:
                                raise ValueError
                        except ValueError:
                            raise CsvConflict(
                                f"转料#{tid}（{t['src_furnace']}→{t['dst_furnace']}）"
                                "需填写正整数分钟的调阅耗时，或勾选“无法调阅”。"
                            )
                    unknowns.append(UnknownTransfer(
                        tid, t["src_furnace"], t["dst_furnace"], minutes,
                    ))
                plan = plan_retrieval(unknowns)
                with db.transaction(conn):
                    db.save_recovery_plan(
                        conn, ledger_id, plan.ok, plan.total_minutes,
                        plan.cycle_note or None,
                        [(u.tid, u.minutes, u.tid in set(plan.retrieve_ids))
                         for u in unknowns],
                    )
                    if plan.ok:
                        db.log_event(
                            conn, "recovery_plan",
                            f"账套#{ledger_id} 调档清单 {len(plan.retrieve_ids)} 张，"
                            f"总耗时 {plan.total_minutes} 分钟", True,
                        )
                    else:
                        db.log_event(
                            conn, "recovery_plan",
                            f"账套#{ledger_id} 无法推全：{plan.cycle_note}", True,
                        )
            finally:
                conn.close()
        except CsvConflict as exc:
            flash(f"清单保存失败，已有清单与恢复结果保留：{exc}", "error")
            _log_failure("recovery_plan", f"账套#{ledger_id} 清单被拒：{exc}")
            return redirect(url_for("index", ledger=ledger_id, _anchor="recovery"))
        if plan.ok:
            flash(
                f"调档清单已保存：需调阅 {len(plan.retrieve_ids)} 张磅单，"
                f"总耗时 {plan.total_minutes} 分钟（已是最少）；"
                f"其余 {len(plan.keep_ids)} 笔待推算。", "ok",
            )
        else:
            flash("可调阅磅单全部找回仍无法推全：" + plan.cycle_note, "error")
        return redirect(url_for("index", ledger=ledger_id, _anchor="recovery"))

    # ------------------------------------------------------------------
    @app.route("/recovery/entry", methods=["POST"])
    def recovery_entry():
        """一次录齐清单实称公斤数，推算其余并另存逐笔恢复重量。

        任一校验失败即整体回滚：已有清单与既有恢复结果全部保留。
        """
        ledger_id = request.form.get("ledger_id", type=int)
        try:
            conn = db.get_conn()
            try:
                with db.transaction(conn):
                    ledger = conn.execute(
                        "SELECT * FROM ledgers WHERE id = ?", (ledger_id,)
                    ).fetchone()
                    if not ledger:
                        raise CsvConflict("账套不存在。")
                    rec_list, items = db.load_recovery_plan(conn, ledger_id)
                    if not rec_list:
                        raise CsvConflict("该账套还没有调档清单，请先勾选生成。")
                    if not rec_list["feasible"]:
                        raise CsvConflict("当前清单无法推全，请调整勾选后重新生成。")
                    measured = {}
                    for it in items:
                        if not it["retrieve"]:
                            continue
                        tid = it["transfer_id"]
                        raw = (request.form.get(f"act_{tid}") or "").strip()
                        try:
                            v = int(raw)
                            if v < 0:
                                raise ValueError
                        except ValueError:
                            raise CsvConflict(
                                f"请一次录齐：转料#{tid} 的实称公斤数必须是非负整数。"
                            )
                        measured[tid] = v
                    furnaces, batches, transfers = db.load_ledger(conn, ledger_id)
                    checked_ids = {i["transfer_id"] for i in items}
                    f_in = [(f["code"], f["primary_kg"], f["recycled_kg"])
                            for f in furnaces]
                    b_in = [(b["furnace_code"], b["weight_kg"]) for b in batches]
                    known, unknown = [], []
                    for t in transfers:
                        tid = t["id"]
                        if tid in checked_ids:
                            if tid in measured:
                                known.append((t["src_furnace"], t["dst_furnace"],
                                              measured[tid]))
                            else:
                                unknown.append((tid, t["src_furnace"],
                                                t["dst_furnace"]))
                        else:
                            known.append((t["src_furnace"], t["dst_furnace"],
                                          t["weight_kg"]))
                    try:
                        derived = derive_weights(f_in, b_in, known, unknown)
                    except RecoveryError as exc:
                        raise CsvConflict(str(exc))
                    all_t = known + [
                        (src, dst, derived[tid]) for tid, src, dst in unknown
                    ]
                    problem = check_all_balanced(f_in, b_in, all_t)
                    if problem:
                        raise CsvConflict(problem)
                    db.save_recovery_actuals(conn, ledger_id, measured.items())
                    values = [(tid, kg, "实查") for tid, kg in sorted(measured.items())]
                    values += [(tid, kg, "推算") for tid, kg in sorted(derived.items())]
                    db.save_recovery_values(conn, ledger_id, values)
                    db.log_event(
                        conn, "recovery_entry",
                        f"账套#{ledger_id} 恢复 {len(values)} 笔"
                        f"（实查 {len(measured)}，推算 {len(derived)}）", True,
                    )
            finally:
                conn.close()
        except CsvConflict as exc:
            flash(f"恢复保存失败，已有清单与恢复结果保留：{exc}", "error")
            _log_failure("recovery_entry", f"账套#{ledger_id} 录入被拒（回滚）：{exc}")
            return redirect(url_for("index", ledger=ledger_id, _anchor="recovery"))
        flash(
            f"恢复完成：实查 {len(measured)} 笔、推算 {len(derived)} 笔，"
            "逐笔重量已另存并标明来源。", "ok",
        )
        return redirect(url_for("index", ledger=ledger_id, _anchor="recovery"))

    # ------------------------------------------------------------------
    @app.route("/recovery/clear", methods=["POST"])
    def recovery_clear():
        """清空该账套的调档清单与恢复结果，重新勾选。"""
        ledger_id = request.form.get("ledger_id", type=int)
        conn = db.get_conn()
        try:
            with db.transaction(conn):
                db.clear_recovery(conn, ledger_id)
                db.log_event(
                    conn, "recovery_clear",
                    f"账套#{ledger_id} 清单与恢复结果已清空", True,
                )
            flash("已清空该账套的调档清单与恢复结果，可重新勾选。", "ok")
        finally:
            conn.close()
        return redirect(url_for("index", ledger=ledger_id, _anchor="recovery"))

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.errorhandler(413)
    def too_large(_e):
        flash("上传文件超过 8MB 限制。", "error")
        return redirect(url_for("index"))

    return app


class CsvConflict(Exception):
    """事务内业务冲突，触发回滚并向页面提示原因。"""
