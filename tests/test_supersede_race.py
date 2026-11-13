"""候选替换（重新求解）竞态回归测试。

背景缺陷（修复前）：
- solve_controls 是全局单行表，旧 worker 在任何收尾路径上都会把信号行清空，
  可能把"新任务"的暂停/取消信号一起消费掉；
- worker 写 solved/paused/cancelled/failed 时只按 id 更新、没有归属与状态保护，
  新求解把旧候选置为 superseded 后，旧线程迟到的收尾会把它改回活跃态（复活）。

本文件用一个可由测试线程控制返回时机的桩求解器（替换 manager.build_and_solve），
确定性地复现"旧求解迟到成功/暂停/取消/异常"与控制操作交错的全部场景。
"""
from __future__ import annotations

import threading
import time

import pytest

import app.manager as manager_mod
from app.solver import SolveAborted, SolvePaused

QUOTA = {
    "glioma":     {"train": 7, "validation": 5, "test": 4},
    "meningioma": {"train": 8, "validation": 4, "test": 3},
    "pituitary":  {"train": 6, "validation": 4, "test": 2},
    "normal":     {"train": 9, "validation": 4, "test": 4},
}

# 与 tests/test_api.py 相同的 60 行虚构数据
SEED_ROWS = []
for sid, disease, n in [
    ("sub-001", "glioma", 7), ("sub-002", "glioma", 5), ("sub-003", "glioma", 4),
    ("sub-004", "meningioma", 8), ("sub-005", "meningioma", 4),
    ("sub-006", "meningioma", 3),
    ("sub-007", "pituitary", 6), ("sub-008", "pituitary", 4),
    ("sub-009", "pituitary", 2),
    ("sub-010", "normal", 9), ("sub-011", "normal", 4), ("sub-012", "normal", 4),
]:
    for i in range(1, n + 1):
        SEED_ROWS.append(
            {"sample_id": f"{sid}_slice-{i:03d}", "subject_id": sid, "disease": disease}
        )


class Gate:
    """桩求解器的阻塞点：在放行前模拟一段持续运行的求解。

    entered 在桩求解器实际进入阻塞循环后置位，测试据此确认任务已被卡住，
    再发送控制信号，避免"信号早于阻塞轮询到达"的时序歧义。
    """

    def __init__(self) -> None:
        self.event = threading.Event()
        self.entered = threading.Event()

    def wait(self, timeout: float = 10.0) -> None:
        if not self.event.wait(timeout):
            raise AssertionError("gate was not released in time")

    def release(self) -> None:
        self.event.set()


def _wait_gate_entered(gate: Gate, timeout: float = 5.0) -> None:
    if not gate.entered.wait(timeout):
        raise AssertionError("solver never entered the gate")


def _install_gated_stub(monkeypatch, gates: dict[int, Gate]) -> None:
    """替换 build_and_solve：任务在放行事件上阻塞，放行后返回 solved。

    - 阻塞期间持续回调 on_tick（心跳）并检查 pause/cancel 信号；
    - 未登记 gate 的任务（例如无需控制的新任务）立即成功返回。
    """

    def fake_solve(snapshot, quota, time_limit_seconds, min_hold_seconds,
                   control=None, already_spent=0.0, on_tick=None,
                   gate_token=None):
        # manager 以 str(solution_id) 作为 gate_token
        sid = int(gate_token) if gate_token is not None else -1
        gate = gates.get(sid)
        if gate is not None:
            gate.entered.set()
            while not gate.event.is_set():
                if on_tick is not None:
                    on_tick()
                if control is not None and control.is_cancelled():
                    raise SolveAborted()
                if control is not None and control.is_paused():
                    control.elapsed_search = already_spent + 0.25
                    raise SolvePaused()
                time.sleep(0.02)
        return {
            "status": "solved",
            "assignments": {},
            "search_time_seconds": 0.5,
            "wall_time_seconds": 0.5,
        }

    monkeypatch.setattr(manager_mod, "build_and_solve", fake_solve)


def _import(client):
    r = client.post("/api/samples/import", json={"samples": SEED_ROWS})
    assert r.status_code == 200, r.text


def _submit(client, min_hold=30.0):
    r = client.post("/api/solve", json={
        "quota": QUOTA, "time_limit_seconds": 60, "min_hold_seconds": min_hold,
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _wait_status(client, sid, targets, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/solve/{sid}").json()
        if body["status"] in targets:
            return body
        time.sleep(0.05)
    raise AssertionError(f"task {sid} never reached {targets}, "
                         f"now={client.get(f'/api/solve/{sid}').json()}")


def _wait_claimed(client, sid):
    _wait_status(client, sid, {"solving", "paused", "solved", "cancelled", "failed"})


# ---------------------------------------------------------------------------
# 1. 旧求解"迟到成功"不得复活 superseded 候选，发布返回 409
# ---------------------------------------------------------------------------
def test_late_success_of_old_solver_cannot_revive_superseded(client, monkeypatch):
    _import(client)
    gates: dict[int, Gate] = {}
    _install_gated_stub(monkeypatch, gates)

    old_id = _submit(client)
    gates[old_id] = Gate()
    _wait_claimed(client, old_id)
    assert client.get(f"/api/solve/{old_id}").json()["status"] == "solving"

    # 新求解受理：旧候选立即 superseded
    new_id = _submit(client, min_hold=0.0)
    assert new_id > old_id
    assert client.get(f"/api/solve/{old_id}").json()["status"] == "superseded"
    # 新任务未装 gate，应很快 solved，且全程不受旧线程影响
    new_result = _wait_status(client, new_id, {"solved"})
    assert new_result["status"] == "solved"

    # 旧线程此时仍阻塞在 gate 上；放行 -> 迟到成功写入必须空转
    gates[old_id].release()
    time.sleep(0.5)
    assert client.get(f"/api/solve/{old_id}").json()["status"] == "superseded"

    # 旧候选发布 -> 409；新候选可正常发布
    r = client.post(f"/api/solve/{old_id}/publish")
    assert r.status_code == 409
    r = client.post(f"/api/solve/{new_id}/publish")
    assert r.status_code == 200, r.text
    # 最新候选视图指向新候选，旧候选不会冒头
    assert client.get("/api/candidate").json()["id"] == new_id


# ---------------------------------------------------------------------------
# 2. 旧求解"迟到暂停"不得把 superseded 改回 paused
# ---------------------------------------------------------------------------
def test_late_pause_of_old_solver_cannot_revive_superseded(client, monkeypatch):
    _import(client)
    gates: dict[int, Gate] = {}
    _install_gated_stub(monkeypatch, gates)

    old_id = _submit(client)
    gates[old_id] = Gate()
    _wait_claimed(client, old_id)
    _wait_gate_entered(gates[old_id])

    # 在旧任务仍被 gate 卡住时发暂停（信号已落库，但桩要等下一轮检查）
    assert client.post(f"/api/solve/{old_id}/pause").status_code == 200

    # 新求解受理 -> 旧候选 superseded，旧任务的控制行被提交事务清掉
    new_id = _submit(client, min_hold=0.0)
    assert client.get(f"/api/solve/{old_id}").json()["status"] == "superseded"
    _wait_status(client, new_id, {"solved"})

    # 放行旧线程：它在 gate 处醒来，即便先观察到 pause 也不能写回 paused
    gates[old_id].release()
    time.sleep(0.5)
    assert client.get(f"/api/solve/{old_id}").json()["status"] == "superseded"


# ---------------------------------------------------------------------------
# 3. 旧求解"迟到取消"不得覆盖 superseded，也不得清除新任务信号
# ---------------------------------------------------------------------------
def test_late_cancel_of_old_solver_is_isolated(client, monkeypatch):
    _import(client)
    gates: dict[int, Gate] = {}
    _install_gated_stub(monkeypatch, gates)

    old_id = _submit(client)
    gates[old_id] = Gate()
    _wait_claimed(client, old_id)

    new_id = _submit(client)  # 新任务也装 gate，保持运行以便挂暂停信号
    gates[new_id] = Gate()
    assert client.get(f"/api/solve/{old_id}").json()["status"] == "superseded"
    _wait_claimed(client, new_id)
    _wait_gate_entered(gates[new_id])

    # 给新任务挂暂停信号，然后让旧任务"迟到取消"
    assert client.post(f"/api/solve/{new_id}/pause").status_code == 200
    # 旧任务已 superseded，控制接口必须直接拒绝（409），无法写入任何信号
    r = client.post(f"/api/solve/{old_id}/cancel")
    assert r.status_code == 409

    gates[old_id].release()  # 旧线程醒来收尾
    time.sleep(0.4)
    assert client.get(f"/api/solve/{old_id}").json()["status"] == "superseded"

    # 新任务的暂停信号仍然存在并生效：放行后它应进入 paused 而不是 solved
    gates[new_id].release()
    paused_body = _wait_status(client, new_id, {"paused"})
    assert paused_body["status"] == "paused"

    # 新候选仍可恢复、完成并发布
    assert client.post(f"/api/solve/{new_id}/resume").status_code == 200
    # resume 后桩立即返回 solved
    solved = _wait_status(client, new_id, {"solved"})
    assert solved["status"] == "solved"
    assert client.post(f"/api/solve/{new_id}/publish").status_code == 200


# ---------------------------------------------------------------------------
# 4. 旧求解"迟到异常"不得把 superseded 改成 failed
# ---------------------------------------------------------------------------
def test_late_exception_of_old_solver_cannot_revive(client, monkeypatch):
    _import(client)
    gates: dict[int, Gate] = {}
    _install_gated_stub(monkeypatch, gates)
    fail_ids: set[int] = set()  # old_id 加入后，其迟到收尾改为抛异常

    def fake_solve(snapshot, quota, time_limit_seconds, min_hold_seconds,
                   control=None, already_spent=0.0, on_tick=None,
                   gate_token=None):
        sid = int(gate_token) if gate_token is not None else -1
        gate = gates.get(sid)
        if gate is not None:
            gate.entered.set()
            while not gate.event.is_set():
                if on_tick is not None:
                    on_tick()
                if control is not None and control.is_cancelled():
                    raise SolveAborted()
                time.sleep(0.02)
        if sid in fail_ids:
            raise RuntimeError("injected late solver crash")
        return {"status": "solved", "assignments": {},
                "search_time_seconds": 0.5, "wall_time_seconds": 0.5}

    monkeypatch.setattr(manager_mod, "build_and_solve", fake_solve)

    old_id = _submit(client)
    gates[old_id] = Gate()
    fail_ids.add(old_id)
    _wait_claimed(client, old_id)

    new_id = _submit(client, min_hold=0.0)
    _wait_status(client, new_id, {"solved"})
    assert client.get(f"/api/solve/{old_id}").json()["status"] == "superseded"

    gates[old_id].release()  # 旧线程抛异常 -> failed 写入必须空转
    time.sleep(0.5)
    assert client.get(f"/api/solve/{old_id}").json()["status"] == "superseded"
    # 新候选不受牵连
    assert client.get(f"/api/solve/{new_id}").json()["status"] == "solved"


# ---------------------------------------------------------------------------
# 5. 交错控制：新任务暂停期间，旧任务的迟到收尾不能消费新任务的暂停信号
# ---------------------------------------------------------------------------
def test_interleaved_controls_new_task_signal_survives(client, monkeypatch):
    _import(client)
    gates: dict[int, Gate] = {}
    _install_gated_stub(monkeypatch, gates)

    old_id = _submit(client)
    gates[old_id] = Gate()
    _wait_claimed(client, old_id)
    _wait_gate_entered(gates[old_id])

    new_id = _submit(client)
    gates[new_id] = Gate()
    _wait_claimed(client, new_id)
    _wait_gate_entered(gates[new_id])
    assert client.get(f"/api/solve/{old_id}").json()["status"] == "superseded"

    # 新任务暂停
    assert client.post(f"/api/solve/{new_id}/pause").status_code == 200
    _wait_status(client, new_id, {"paused"})

    # 旧线程此刻才迟到成功 —— 不能动新任务的 paused 状态与控制行
    gates[old_id].release()
    time.sleep(0.5)
    assert client.get(f"/api/solve/{old_id}").json()["status"] == "superseded"
    assert client.get(f"/api/solve/{new_id}").json()["status"] == "paused"

    # 对已失效旧候选的任何控制请求都是 409
    for sig in ("pause", "resume", "cancel"):
        assert client.post(f"/api/solve/{old_id}/{sig}").status_code == 409

    # 新任务恢复 -> 取消也正常工作
    assert client.post(f"/api/solve/{new_id}/resume").status_code == 200
    assert client.post(f"/api/solve/{new_id}/cancel").status_code == 200
    _wait_status(client, new_id, {"cancelled"})
