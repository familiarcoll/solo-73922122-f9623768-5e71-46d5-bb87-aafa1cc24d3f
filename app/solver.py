"""OR-Tools CP-SAT 分组求解器。

约束：
1. 每个受试者的全部样本必须归入同一个集合（train/validation/test）；
2. 各病种在三个集合中的样本数必须精确等于请求配额；
3. 被冻结的受试者只能进入其冻结集合。

本模块是纯函数式的：输入完整快照与配额，输出分配结果或"无解"。
求解期间不访问数据库，因此不持有任何数据库写锁；
暂停 / 继续 / 取消通过传入的控制回调实现，控制信号由上层从
solve_controls 表轮询得到，因此两个 API 进程都可以控制对方发起的求解。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

from ortools.sat.python import cp_model

from .config import GROUPS


class SolveAborted(Exception):
    """收到 cancel 信号。"""


class SolvePaused(Exception):
    """收到 pause 信号，已消耗的搜索时间随状态一起返回。"""


# ---------------------------------------------------------------------------
# 演示用"求解闸门"：把某个任务的成功返回时机交给外部控制。
#
# 当环境变量 SLICE_SOLVE_GATE_DIR 指向一个目录时，worker 在已经找到可行解、
# 准备返回成功之前会阻塞，直到该目录下出现 release-<gate_token> 文件
# （gate_token 即 solution_id）。闸门等待期间仍按分片节奏执行 on_tick
# （刷新心跳）并响应 pause/cancel，因此暂停/取消语义不受影响。
# 默认不设置该环境变量，求解行为与普通 CP-SAT 完全一致。
# ---------------------------------------------------------------------------
_GATE_POLL_SECONDS = 0.1


def _wait_gate(
    gate_token: str | None,
    control: "_ControlState | None",
    on_tick: Callable[[], None] | None,
) -> None:
    if gate_token is None:
        return
    gate_dir = os.environ.get("SLICE_SOLVE_GATE_DIR")
    if not gate_dir:
        return
    release_file = os.path.join(gate_dir, f"release-{gate_token}")
    while not os.path.exists(release_file):
        if on_tick is not None:
            on_tick()
        if control is not None and control.is_cancelled():
            raise SolveAborted()
        if control is not None and control.is_paused():
            control.elapsed_search = getattr(control, "elapsed_search", 0.0)
            raise SolvePaused()
        time.sleep(_GATE_POLL_SECONDS)


class _ControlState:
    """线程内控制状态，由 monitor 回调驱动。"""

    def __init__(
        self,
        is_cancelled: Callable[[], bool],
        is_paused: Callable[[], bool],
    ):
        self.is_cancelled = is_cancelled
        self.is_paused = is_paused
        self.elapsed_search = 0.0
        self.lock = threading.Lock()


class _Recorder(cp_model.CpSolverSolutionCallback):
    """记录最近一个可行解（可行性问题，任意可行解即满足全部硬约束）。

    注意：回调内禁止让 Python 异常逃逸到 C++ 层，否则 OR-Tools 会直接段错误，
    因此这里只对标量 IntVar 调用 Value，并自行从指示变量推导归组。
    """

    def __init__(self, assign_vars: dict[str, list[cp_model.IntVar]]):
        super().__init__()
        self._vars = assign_vars
        self.best: dict[str, int] = {}

    def on_solution_callback(self) -> None:
        picked: dict[str, int] = {}
        for sid, indicators in self._vars.items():
            for gi, bool_var in enumerate(indicators):
                if self.Value(bool_var):
                    picked[sid] = gi
                    break
        self.best = picked


def build_and_solve(
    snapshot: dict[str, Any],
    quota: dict[str, dict[str, int]],
    time_limit_seconds: float,
    min_hold_seconds: float,
    control: _ControlState | None = None,
    already_spent: float = 0.0,
    on_tick: Callable[[], None] | None = None,
    gate_token: str | None = None,
) -> dict[str, Any]:
    """执行求解。

    返回:
        {"status": "solved", "assignments": {subject_id: group}, ...}
        {"status": "infeasible", ...}
        暂停/取消时抛出 SolvePaused / SolveAborted。
    """
    samples = snapshot["samples"]
    freezes = snapshot["freezes"]

    # 受试者 -> 病种 -> 样本数
    subjects: dict[str, dict[str, int]] = {}
    for smp in samples:
        subjects.setdefault(smp["subject_id"], {})
        subjects[smp["subject_id"]][smp["disease"]] = (
            subjects[smp["subject_id"]].get(smp["disease"], 0) + 1
        )

    model = cp_model.CpModel()
    group_index = {g: i for i, g in enumerate(GROUPS)}

    # x[s] in {0,1,2}：受试者 s 的归组
    x: dict[str, cp_model.IntVar] = {}
    assign_indicator: dict[str, list[cp_model.IntVar]] = {}
    for sid in sorted(subjects):
        x[sid] = model.new_int_var(0, len(GROUPS) - 1, f"group_{sid}")
        indicators = []
        for gi, gname in enumerate(GROUPS):
            b = model.new_bool_var(f"{sid}=={gname}")
            model.add(x[sid] == gi).only_enforce_if(b)
            indicators.append(b)
        # 恰好落在一个集合
        model.add_exactly_one(indicators)
        assign_indicator[sid] = indicators
        if sid in freezes:
            model.add(x[sid] == group_index[freezes[sid]])

    # 精确配额：每个 (病种, 集合) 的样本总数等于配额
    diseases = sorted({smp["disease"] for smp in samples})
    for disease in diseases:
        for gi, gname in enumerate(GROUPS):
            target = quota[disease][gname]
            terms = []
            for sid, disease_counts in subjects.items():
                cnt = disease_counts.get(disease, 0)
                if cnt:
                    terms.append(assign_indicator[sid][gi] * cnt)
            if terms:
                model.add(sum(terms) == target)
            elif target != 0:
                # 没有任何受试者贡献该病种，却要求非零配额，模型直接无解
                model.add(1 == 0)

    recorder = _Recorder(assign_indicator)

    # ---- 控制机制 -------------------------------------------------------
    # OR-Tools 没有原生 pause API。采用"分片求解"：每次给 solver 一小段
    # 时间预算，在每个分片前后都检查 pause/cancel 信号；暂停期间原地等待，
    # 恢复后带着剩余预算继续。注意每个分片必须新建 CpSolver——
    # 在同一个 solver/model 上重复调用 solve() 会触发其 C++ 层的段错误；
    # model 与 recorder（已找到的可行解）可以安全复用。
    slice_seconds = 0.25
    remaining = max(0.1, float(time_limit_seconds) - already_spent)
    hold_left = max(0.0, float(min_hold_seconds) - already_spent)

    start_wall = time.monotonic()
    search_used = already_spent
    last_status = None

    def check_signals() -> None:
        if on_tick is not None:
            on_tick()
        if control is not None and control.is_cancelled():
            raise SolveAborted()
        if control is not None and control.is_paused():
            # 保存当前已消耗的搜索时间后挂起，等待 resume
            control.elapsed_search = search_used
            raise SolvePaused()

    while True:
        budget = min(slice_seconds, remaining)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = budget
        solver.parameters.num_search_workers = 2
        solver.parameters.log_search_progress = False
        status = solver.solve(model, recorder)
        last_status = status
        search_used += budget
        remaining -= budget

        # 分片结束后立刻检查信号：暂停会记录到刚消耗的预算（spent > 0）
        check_signals()

        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            # 可行性问题：找到可行解即满足全部硬约束。
            # min_hold_seconds 仅用于演示可控暂停，让求解过程保持存活。
            if hold_left <= 0:
                # 演示闸门（默认关闭）：把"成功返回"的时机交给外部，
                # 用于在双进程环境中确定性地制造"旧求解迟到成功"的交错场景。
                _wait_gate(gate_token, control, on_tick)
                break
        if status == cp_model.INFEASIBLE:
            return {
                "status": "infeasible",
                "detail": "CP-SAT 证明在当前样本、冻结与配额下不存在可行分组",
                "search_time_seconds": round(search_used, 3),
            }
        if remaining <= 0.05:
            if recorder.best:
                _wait_gate(gate_token, control, on_tick)
                break
            return {
                "status": "infeasible",
                "detail": f"时间预算 {time_limit_seconds}s 内未找到可行解（最后状态 {solver.status_name(status)}）",
                "search_time_seconds": round(search_used, 3),
            }
        if hold_left > 0:
            hold_left -= budget

    best = recorder.best
    assignments = {sid: GROUPS[gi] for sid, gi in sorted(best.items())}
    return {
        "status": "solved",
        "assignments": assignments,
        "search_time_seconds": round(search_used, 3),
        "wall_time_seconds": round(time.monotonic() - start_wall, 3),
    }
