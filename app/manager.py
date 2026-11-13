"""求解任务管理：后台监督线程、跨进程认领、暂停/继续/取消。

两个 API 进程各自运行一个 SolveManager。求解任务行写入共享 SQLite 后：
- 本进程立刻尝试认领；若本进程没有领到（例如在另一进程提交），
  另一进程的监督线程会在 1 秒内扫描并原子认领；
- 控制信号（pause/resume/cancel）写入 solve_controls 表，每个任务一行、
  按 solution_id 严格隔离，worker 每个求解分片轮询自己那一行，
  因此任一进程都能控制任一任务，且旧任务的收尾不可能清掉新任务的信号；
- 心跳（heartbeat）超时的任务会被其他进程重新认领，单进程崩溃不会卡死任务。

候选替换（重新求解）的防复活不变量：
- submit 在同一事务里把旧候选置为 superseded，并只删除旧任务自己的控制行；
- worker 的所有状态写入都经过 database.guarded_transition，仅当任务仍归属
  本 worker 且仍处于活跃状态（solving/paused）时才生效；旧线程迟到的成功、
  暂停、取消、异常收尾在任务被取代/接管后全部空转，superseded 终态不可覆盖；
- 心跳刷新同样带 worker 归属条件。

求解线程只在任务开始/结束时短暂访问数据库，CP-SAT 计算期间不持任何锁，
因此求解进行中可以继续导入样本、修改病种或冻结归组。
"""
from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from typing import Any

from . import database as db
from .errors import ServiceError
from .solver import SolveAborted, SolvePaused, _ControlState, build_and_solve

HEARTBEAT_TIMEOUT_SECONDS = 30.0
SCAN_INTERVAL_SECONDS = 1.0


class SolveManager:
    def __init__(self) -> None:
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        # 本进程正在运行的任务 id 集合（占位字典，控制信号以数据库为准）
        self._local: dict[int, dict] = {}
        self._local_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._supervise, name="solve-supervisor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def reset(self) -> None:
        """测试夹具使用：恢复到未启动状态。"""
        self.stop()
        self._stop = threading.Event()
        self._thread = None
        with self._local_lock:
            self._local.clear()

    # ------------------------------------------------------------------
    # 提交 / 控制
    # ------------------------------------------------------------------
    def submit(self, quota_request: dict[str, Any], time_limit: float,
               min_hold: float) -> dict[str, Any]:
        """校验配额、写入快照、登记求解任务。返回任务信息。"""
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            snapshot = db.take_snapshot(conn)
            if not snapshot["samples"]:
                raise ServiceError(422, "数据库中没有样本，无法求解")
            disease_totals: dict[str, int] = {}
            for smp in snapshot["samples"]:
                disease_totals[smp["disease"]] = disease_totals.get(smp["disease"], 0) + 1

            from .schemas import validate_quota
            normalized = validate_quota(quota_request, disease_totals)

            # 新候选使旧候选全部作废（标记 superseded；不触碰 published）。
            # 若旧任务正处于 paused，其 worker 还在等待信号，一并置为终态，
            # worker 醒来后发现状态已非活跃即退出，不会继续求解已失效任务。
            conn.execute(
                "UPDATE solutions SET status = 'superseded' "
                "WHERE kind = 'candidate' AND status != 'superseded'"
            )
            # 控制通道按任务隔离：只清掉被取代旧任务的信号行，
            # 绝不能用全局 DELETE 把未来新任务的信号一并抹掉。
            conn.execute(
                """DELETE FROM solve_controls
                    WHERE solution_id IN (
                        SELECT id FROM solutions
                         WHERE kind = 'candidate' AND status = 'superseded')"""
            )
            full_request = {
                "quota": normalized,
                "time_limit_seconds": time_limit,
                "min_hold_seconds": min_hold,
            }
            solution_id = db.create_solution_row(
                conn,
                status="solving",
                input_version=snapshot["data_version"],
                quota_request=full_request,
                snapshot=snapshot,
            )
            conn.execute("COMMIT")
        except Exception:
            db._rollback_safely(conn)
            raise
        finally:
            conn.close()

        # 本进程立即尝试认领，避免等待扫描周期
        self._try_claim(solution_id)
        return self.get_status(solution_id)

    def send_signal(self, solution_id: int, signal: str) -> dict[str, Any]:
        assert signal in ("pause", "resume", "cancel")
        conn = db.get_conn()
        try:
            row = db.get_solution(conn, solution_id)
            if row is None or row["kind"] != "candidate":
                raise ServiceError(404, f"候选求解任务不存在: {solution_id}")
            if row["status"] not in ("solving", "paused"):
                raise ServiceError(
                    409, f"任务当前状态为 {row['status']}，无法接受 {signal} 信号"
                )
            if signal == "pause" and row["status"] != "solving":
                raise ServiceError(409, "任务不在 solving 状态，无法暂停")
            if signal == "resume" and row["status"] != "paused":
                raise ServiceError(409, "任务不在 paused 状态，无法继续")
            if signal == "cancel" and row["status"] not in ("solving", "paused"):
                raise ServiceError(409, "任务已结束，无法取消")
            conn.execute("BEGIN IMMEDIATE")
            db.write_control(conn, solution_id, signal)
            conn.execute("COMMIT")
        except Exception:
            db._rollback_safely(conn)
            raise
        finally:
            conn.close()

        # 信号已落库；worker 在 0.5s 求解分片或 0.2s 等待轮询内即可观察到
        return self.get_status(solution_id)

    def get_status(self, solution_id: int) -> dict[str, Any]:
        conn = db.get_conn()
        try:
            row = db.get_solution(conn, solution_id)
            if row is None:
                raise ServiceError(404, f"求解任务不存在: {solution_id}")
            return self._decorate(conn, db.row_to_dict(row))
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 监督线程
    # ------------------------------------------------------------------
    def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                self._reap_orphans()
                # 已被本进程认领、且尚未启动线程的任务
                conn = db.get_conn()
                try:
                    rows = conn.execute(
                        "SELECT id FROM solutions "
                        "WHERE kind = 'candidate' AND status IN ('solving', 'paused') "
                        "AND worker_id = ? ORDER BY id",
                        (self.worker_id,),
                    ).fetchall()
                finally:
                    conn.close()
                with self._local_lock:
                    running = set(self._local.keys())
                for r in rows:
                    sid = r["id"]
                    if sid not in running:
                        self._spawn(sid)
                # 尝试认领无人负责的新任务
                self._try_claim(None)
            except Exception as exc:  # 监督线程永不退出
                print(f"[solve-manager] supervisor iteration failed: {exc}", flush=True)
            self._stop.wait(SCAN_INTERVAL_SECONDS)

    def _reap_orphans(self) -> None:
        """心跳超时的 solving 任务视为原 worker 死亡，释放给其他进程认领。"""
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """UPDATE solutions
                      SET worker_id = NULL, heartbeat = NULL
                    WHERE kind = 'candidate' AND status = 'solving'
                      AND worker_id IS NOT NULL
                      AND (julianday('now') - julianday(heartbeat)) * 86400 > ?""",
                (HEARTBEAT_TIMEOUT_SECONDS,),
            )
            if cur.rowcount:
                print(f"[solve-manager] reaped {cur.rowcount} orphaned task(s)", flush=True)
            conn.execute("COMMIT")
        except Exception:
            db._rollback_safely(conn)
        finally:
            conn.close()

    def _try_claim(self, preferred_id: int | None) -> None:
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if preferred_id is not None:
                conn.execute(
                    "UPDATE solutions SET worker_id = ?, "
                    "heartbeat = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                    "WHERE id = ? AND worker_id IS NULL AND status = 'solving'",
                    (self.worker_id, preferred_id),
                )
            claimed = db.claim_pending_solution(conn, self.worker_id)
            conn.execute("COMMIT")
        except Exception:
            db._rollback_safely(conn)
            raise
        finally:
            conn.close()
        if claimed is not None:
            self._spawn(claimed)

    def _spawn(self, solution_id: int) -> None:
        with self._local_lock:
            if solution_id in self._local:
                return
            # 占位条目仅用于防止监督线程重复 spawn；控制信号以数据库为准
            self._local[solution_id] = {}
        t = threading.Thread(
            target=self._run, args=(solution_id, {}),
            name=f"solver-{solution_id}", daemon=True,
        )
        t.start()

    # ------------------------------------------------------------------
    # 单个任务的执行
    # ------------------------------------------------------------------
    def _read_signal(self, solution_id: int) -> str | None:
        conn = db.get_conn()
        try:
            # 每个任务只读自己的控制行，旧任务永远看不到新任务的信号
            return db.read_control(conn, solution_id)
        finally:
            conn.close()

    def _run(self, solution_id: int, _entry: dict) -> None:
        """任务主循环。控制信号以 solve_controls 表为唯一权威（跨进程生效）。

        不变量（防"旧求解复活"）：
        - 对 solutions 的每一次状态写入都经过 guarded_transition，
          仅当任务仍归属本 worker 且仍处于活跃状态时才生效；
        - 被新求解取代（superseded）或被其他进程接管后，本线程的任何
          迟到收尾（成功/暂停/取消/异常）都只会空转，状态不会被改回；
        - 每个任务只读写自己的控制行，旧线程的收尾不可能清除新任务的信号。
        """
        try:
            while True:
                signal = self._read_signal(solution_id)
                conn = db.get_conn()
                try:
                    row = conn.execute(
                        "SELECT status, worker_id FROM solutions WHERE id = ?",
                        (solution_id,),
                    ).fetchone()
                finally:
                    conn.close()
                if row is None:
                    return
                status = row["status"]

                # 任务已被新求解取代（superseded）或进入其它终态，或已被
                # 其他进程接管：本 worker 直接退出，不做任何写入。
                if status not in ("solving", "paused") or row["worker_id"] != self.worker_id:
                    return

                if signal == "cancel":
                    self._mark_terminal(solution_id, "cancelled",
                                        error_message="任务已被取消")
                    return

                if signal == "resume" and status == "paused":
                    conn = db.get_conn()
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        moved = db.guarded_transition(
                            conn, solution_id, self.worker_id,
                            ("paused",), status="solving",
                            heartbeat=db.SQLITE_NOW,
                        )
                        if moved:
                            # 只清除本任务自己的控制行
                            db.clear_control(conn, solution_id)
                        conn.execute("COMMIT")
                    except Exception:
                        db._rollback_safely(conn)
                        raise
                    finally:
                        conn.close()
                    continue

                outcome = self._run_once(solution_id)
                if outcome == "paused":
                    # _run_once 已把状态置为 paused 并保留已消耗时间；
                    # 轮询数据库信号，等待 resume/cancel（跨进程同样生效）。
                    while True:
                        sig = self._read_signal(solution_id)
                        if sig in ("resume", "cancel"):
                            break
                        # 任务可能已被新求解标记为 superseded，及时退出
                        c = db.get_conn()
                        try:
                            cur = c.execute(
                                "SELECT status, worker_id FROM solutions WHERE id = ?",
                                (solution_id,),
                            ).fetchone()
                        finally:
                            c.close()
                        if (cur is None
                                or cur["status"] not in ("paused", "solving")
                                or cur["worker_id"] != self.worker_id):
                            return
                        time.sleep(0.2)
                    continue
                return  # solved / infeasible / cancelled / failed / 收尾空转
        finally:
            with self._local_lock:
                self._local.pop(solution_id, None)

    def _run_once(self, solution_id: int) -> str:
        conn = db.get_conn()
        try:
            row = db.get_solution(conn, solution_id)
            if row is None:
                return "failed"
            data = db.row_to_dict(row)
            snapshot = data["input_snapshot"]
            request = data["quota_request"]
            spent = float(row["spent_seconds"] or 0.0)
        finally:
            conn.close()

        # 只认数据库信号；每 0.5s 的求解分片都会触发一次回调检查
        control = _ControlState(
            is_cancelled=lambda: self._read_signal(solution_id) == "cancel",
            is_paused=lambda: self._read_signal(solution_id) == "pause",
        )

        def heartbeat() -> None:
            c = db.get_conn()
            try:
                # 只刷新仍归本 worker 所有的活跃任务；被取代/接管后空转
                db.touch_heartbeat(c, solution_id, self.worker_id)
            finally:
                c.close()

        try:
            result = build_and_solve(
                snapshot=snapshot,
                quota=request["quota"],
                time_limit_seconds=float(request["time_limit_seconds"]),
                min_hold_seconds=float(request["min_hold_seconds"]),
                control=control,
                already_spent=spent,
                on_tick=heartbeat,
                gate_token=str(solution_id),
            )
        except SolveAborted:
            self._mark_terminal(solution_id, "cancelled", error_message="任务已被取消")
            return "cancelled"
        except SolvePaused:
            conn = db.get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                # 关键防护：只有仍归属本 worker 的 solving 任务才能被置为 paused。
                # 若任务此刻已被新求解标记为 superseded，写入空转，旧任务不会复活。
                moved = db.guarded_transition(
                    conn, solution_id, self.worker_id, ("solving",),
                    status="paused",
                    spent_seconds=control.elapsed_search,
                )
                conn.execute("COMMIT")
            except Exception:
                db._rollback_safely(conn)
                raise
            finally:
                conn.close()
            return "paused" if moved else "superseded"
        except Exception as exc:  # 求解器自身异常：同样不允许复活已失效任务
            self._mark_terminal(solution_id, "failed", error_message=str(exc))
            return "failed"

        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if result["status"] == "solved":
                moved = db.guarded_transition(
                    conn, solution_id, self.worker_id, ("solving",),
                    status="solved",
                    assignments=result["assignments"],
                    spent_seconds=result["search_time_seconds"],
                    error_message=None,
                )
            else:
                moved = db.guarded_transition(
                    conn, solution_id, self.worker_id, ("solving",),
                    status="infeasible",
                    spent_seconds=result["search_time_seconds"],
                    error_message=result["detail"],
                )
            if moved:
                # 仅在状态确实由本 worker 收尾时清除本任务自己的控制行；
                # 若任务已被取代则什么都不做，尤其不能触碰新任务的信号。
                db.clear_control(conn, solution_id)
            conn.execute("COMMIT")
        finally:
            conn.close()
        return result["status"] if moved else "superseded"

    def _mark_terminal(self, solution_id: int, status: str,
                       error_message: str | None = None) -> None:
        """终态写入（取消/失败）。同样带归属与活跃状态保护：

        被新求解取代（superseded）或被其他进程接管后，迟到的取消/异常
        收尾不会覆盖终态，也不会清除任何其他任务的控制信号。
        """
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            moved = db.guarded_transition(
                conn, solution_id, self.worker_id, ("solving", "paused"),
                status=status,
                error_message=error_message,
            )
            if moved:
                db.clear_control(conn, solution_id)
            conn.execute("COMMIT")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 视图装饰
    # ------------------------------------------------------------------
    def _decorate(self, conn, data: dict[str, Any]) -> dict[str, Any]:
        version = db.current_version(conn)
        data["stale"] = data["input_version"] != version
        data["current_data_version"] = version
        if data["kind"] == "candidate" and data["status"] == "solved":
            data["publishable"] = not data["stale"]
        return data


# 进程级单例
manager = SolveManager()
