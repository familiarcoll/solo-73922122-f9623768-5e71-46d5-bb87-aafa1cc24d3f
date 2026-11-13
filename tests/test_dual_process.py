"""真正的双进程集成测试：两个独立 uvicorn 进程共享同一个 SQLite 文件。

验证修复目标在真实跨进程环境下成立：
- 进程 2 受理新求解后，进程 1 上的旧候选持续 superseded，发布返回 409；
- 旧求解的"成功返回时机"由 SLICE_SOLVE_GATE_DIR 闸门精确控制，
  放行后迟到成功也不能复活旧候选；
- 新任务可被另一个进程暂停/恢复，旧线程收尾不会消费新任务的控制信号；
- 新候选最终可跨进程发布。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

QUOTA = {
    "glioma":     {"train": 7, "validation": 5, "test": 4},
    "meningioma": {"train": 8, "validation": 4, "test": 3},
    "pituitary":  {"train": 6, "validation": 4, "test": 2},
    "normal":     {"train": 9, "validation": 4, "test": 4},
}

SEED_PLAN = [
    ("sub-001", "glioma", 7), ("sub-002", "glioma", 5), ("sub-003", "glioma", 4),
    ("sub-004", "meningioma", 8), ("sub-005", "meningioma", 4),
    ("sub-006", "meningioma", 3),
    ("sub-007", "pituitary", 6), ("sub-008", "pituitary", 4),
    ("sub-009", "pituitary", 2),
    ("sub-010", "normal", 9), ("sub-011", "normal", 4), ("sub-012", "normal", 4),
]


def _seed_rows():
    rows = []
    for sid, disease, n in SEED_PLAN:
        for i in range(1, n + 1):
            rows.append({"sample_id": f"{sid}_slice-{i:03d}",
                         "subject_id": sid, "disease": disease})
    return rows


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _call(base: str, method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = resp.read()
            return resp.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def _wait(base: str, sid: int, targets: set[str], timeout: float = 30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, body = _call(base, "GET", f"/api/solve/{sid}")
        if body.get("status") in targets:
            return body
        time.sleep(0.2)
    raise AssertionError(f"task {sid} never reached {targets}: {body}")


def _wait_healthy(base: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=5) as resp:
                if resp.status == 200:
                    return
        except OSError:
            time.sleep(0.3)
    raise AssertionError(f"service never became healthy: {base}")


@pytest.fixture()
def dual_process(tmp_path):
    db_path = tmp_path / "shared.db"
    gate_dir = tmp_path / "gates"
    gate_dir.mkdir()

    env = {
        **os.environ,
        "SLICE_DB_PATH": str(db_path),
        "SLICE_SOLVE_GATE_DIR": str(gate_dir),
        "SLICE_ENABLE_SHUTDOWN": "1",
        "PYTHONPATH": str(REPO_ROOT),
    }

    def spawn() -> tuple[str, subprocess.Popen]:
        # 偶发的临时端口占用（前次进程尚未完全退出）时换端口重试
        for _ in range(5):
            port = _free_port()
            base = f"http://127.0.0.1:{port}"
            proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app",
                 "--host", "127.0.0.1", "--port", str(port)],
                cwd=str(REPO_ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            try:
                _wait_healthy(base, timeout=20.0)
            except AssertionError:
                # 端口绑定失败等：清理后换端口再来
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass  # 实际已启动只是健康检查抖动，直接返回
                else:
                    continue
            return base, proc
        raise AssertionError("could not start a healthy uvicorn process")

    base1, proc1 = spawn()
    base2, proc2 = spawn()
    procs = [proc1, proc2]

    def shutdown(base: str, proc: subprocess.Popen) -> None:
        # 优先走 uvicorn 的优雅 HTTP 关停（沙箱可能禁止跨调用发信号）
        try:
            req = urllib.request.Request(base + "/shutdown", method="POST")
            urllib.request.urlopen(req, timeout=5).read()
        except OSError:
            pass
        try:
            proc.wait(timeout=8)
            return
        except subprocess.TimeoutExpired:
            pass
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                proc.send_signal(sig)
            except (PermissionError, ProcessLookupError):
                return
            try:
                proc.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                continue

    try:
        _wait_healthy(base1)
        _wait_healthy(base2)
        yield base1, base2, gate_dir
    finally:
        for base, p in ((base1, procs[0]), (base2, procs[1])):
            shutdown(base, p)


def test_two_process_resupersede_and_cross_process_controls(dual_process):
    base1, base2, gate_dir = dual_process

    # 1) 进程 1 导入数据
    status, body = _call(base1, "POST", "/api/samples/import",
                         {"samples": _seed_rows()})
    assert status == 200, body

    # 2) 进程 1 提交旧求解 A：闸门会拦住它的成功返回
    status, body = _call(base1, "POST", "/api/solve",
                         {"quota": QUOTA, "time_limit_seconds": 30,
                          "min_hold_seconds": 0})
    assert status == 200, body
    old_id = body["id"]
    _wait(base1, old_id, {"solving"})
    # 模型极小，首个分片即可找到可行解；等待其停在闸门上
    time.sleep(2.5)
    assert not (gate_dir / f"release-{old_id}").exists()
    assert _call(base1, "GET", f"/api/solve/{old_id}")[1]["status"] == "solving"

    # 3) 进程 2 受理新求解 B：A 立即 superseded（跨进程可见）
    status, body = _call(base2, "POST", "/api/solve",
                         {"quota": QUOTA, "time_limit_seconds": 30,
                          "min_hold_seconds": 0})
    assert status == 200, body
    new_id = body["id"]
    assert new_id != old_id
    assert _call(base2, "GET", f"/api/solve/{old_id}")[1]["status"] == "superseded"
    assert _call(base1, "GET", f"/api/solve/{old_id}")[1]["status"] == "superseded"

    # 4) 旧候选发布 -> 409（在两个进程上都一样）
    assert _call(base1, "POST", f"/api/solve/{old_id}/publish")[0] == 409
    assert _call(base2, "POST", f"/api/solve/{old_id}/publish")[0] == 409
    # 对旧候选的控制请求同样被拒绝
    for sig in ("pause", "resume", "cancel"):
        assert _call(base2, "POST", f"/api/solve/{old_id}/{sig}")[0] == 409

    # 5) B 同样停在自己的闸门上；用进程 1 暂停进程 2 认领的任务
    time.sleep(2.5)
    assert _call(base1, "GET", f"/api/solve/{new_id}")[1]["status"] == "solving"
    assert _call(base1, "POST", f"/api/solve/{new_id}/pause")[0] == 200
    paused = _wait(base2, new_id, {"paused"})
    assert paused["status"] == "paused"

    # 6) 放行旧求解 A：迟到成功不得复活 superseded，也不得动 B 的暂停状态/信号
    (gate_dir / f"release-{old_id}").touch()
    time.sleep(1.5)
    assert _call(base1, "GET", f"/api/solve/{old_id}")[1]["status"] == "superseded"
    assert _call(base2, "GET", f"/api/solve/{new_id}")[1]["status"] == "paused"

    # 7) 进程 2 恢复 B，再放行 B 的闸门 -> solved
    assert _call(base2, "POST", f"/api/solve/{new_id}/resume")[0] == 200
    (gate_dir / f"release-{new_id}").touch()
    solved = _wait(base1, new_id, {"solved"})
    assert solved["status"] == "solved"

    # 8) 跨进程发布新候选
    status, pub = _call(base1, "POST", f"/api/solve/{new_id}/publish")
    assert status == 200, pub
    assert _call(base2, "GET", "/api/published")[1]["id"] == pub["id"]

    # 9) 再提交 C 并在闸门前取消：取消语义不受影响
    status, body = _call(base2, "POST", "/api/solve",
                         {"quota": QUOTA, "time_limit_seconds": 30})
    c_id = body["id"]
    _wait(base2, c_id, {"solving"})
    assert _call(base1, "POST", f"/api/solve/{c_id}/cancel")[0] == 200
    cancelled = _wait(base1, c_id, {"cancelled"})
    assert cancelled["status"] == "cancelled"
    # B 的发布结果保持不变
    assert _call(base2, "GET", "/api/published")[1]["id"] == pub["id"]
