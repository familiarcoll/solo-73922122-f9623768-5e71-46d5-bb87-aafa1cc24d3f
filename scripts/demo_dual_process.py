"""双进程重新求解竞态演示（可控制旧求解的返回时机）。

场景对应线上事故：医学影像数据工程师调整病种配额、重新划分训练集后，
旧求解线程迟到的成功收尾把已被取代的候选"复活"并可能被误发布，
还会清掉新任务的暂停信号。本脚本在两个共享同一 SQLite 的 API 进程之间
确定性地复现整个交错过程，并验证修复后的隔离语义。

前置：
- 两个 API 进程已启动且设置了同一个 SLICE_SOLVE_GATE_DIR（见 docker-compose.yml）；
- worker 在"已找到可行解、即将返回成功"前会停在闸门目录上，
  等待出现 release-<solution_id> 文件，旧求解何时成功完全由本脚本控制。

用法：
    python scripts/demo_dual_process.py [base_url_1] [base_url_2]
默认：
    base_url_1=http://127.0.0.1:8001
    base_url_2=http://127.0.0.1:8002

脚本只用标准库，可在容器外直接运行；
容器内可执行：docker compose run --rm demo。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE1 = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8001"
BASE2 = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8002"

# 与 docker-compose.yml 中两个 API 的 SLICE_SOLVE_GATE_DIR 一致
GATE_DIR = os.environ.get("SLICE_SOLVE_GATE_DIR", "/tmp/slice-gates")

QUOTA = {
    "glioma":     {"train": 7, "validation": 5, "test": 4},
    "meningioma": {"train": 8, "validation": 4, "test": 3},
    "pituitary":  {"train": 6, "validation": 4, "test": 2},
    "normal":     {"train": 9, "validation": 4, "test": 4},
}


def call(base: str, method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = resp.read()
            return resp.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def wait_status(base: str, sid: int, targets: set[str], timeout: float = 30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, body = call(base, "GET", f"/api/solve/{sid}")
        if body.get("status") in targets:
            return body
        time.sleep(0.25)
    raise TimeoutError(f"task {sid} never reached {targets}: {body}")


def gate_file(sid: int) -> str:
    return os.path.join(GATE_DIR, f"release-{sid}")


def release(sid: int) -> None:
    os.makedirs(GATE_DIR, exist_ok=True)
    with open(gate_file(sid), "w", encoding="utf-8") as fh:
        fh.write(f"released at {time.time()}\n")
    print(f"   >> 闸门放行：创建 {gate_file(sid)}（旧/新求解此刻才允许成功返回）")


def step(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    print(f"进程1: {BASE1}    进程2: {BASE2}    闸门目录: {GATE_DIR}")
    for base in (BASE1, BASE2):
        status, _ = call(base, "GET", "/health")
        assert status == 200, f"{base} 未就绪"
    os.makedirs(GATE_DIR, exist_ok=True)
    # 清掉历史闸门文件，避免污染本次演示
    for name in os.listdir(GATE_DIR):
        if name.startswith("release-"):
            os.remove(os.path.join(GATE_DIR, name))

    # 1. 进程1导入种子数据（仓库自带虚构 CSV 的等价 JSON）
    step("1. 进程1导入 60 个虚构切片样本")
    rows = []
    for sub, disease, n in [
        ("sub-001", "glioma", 7), ("sub-002", "glioma", 5), ("sub-003", "glioma", 4),
        ("sub-004", "meningioma", 8), ("sub-005", "meningioma", 4),
        ("sub-006", "meningioma", 3),
        ("sub-007", "pituitary", 6), ("sub-008", "pituitary", 4),
        ("sub-009", "pituitary", 2),
        ("sub-010", "normal", 9), ("sub-011", "normal", 4), ("sub-012", "normal", 4),
    ]:
        for i in range(1, n + 1):
            rows.append({"sample_id": f"{sub}_slice-{i:03d}",
                         "subject_id": sub, "disease": disease})
    status, body = call(BASE1, "POST", "/api/samples/import", {"samples": rows})
    assert status == 200, body
    print("   导入结果:", body)

    # 2. 进程1提交旧求解 A —— 它会停在闸门上，成功返回时机由我们控制
    step("2. 进程1提交旧求解 A（成功返回被闸门拦住）")
    status, body = call(BASE1, "POST", "/api/solve",
                        {"quota": QUOTA, "time_limit_seconds": 30})
    assert status == 200, body
    old_id = body["id"]
    print(f"   旧任务 A id={old_id}，等待其进入 solving 并停在闸门...")
    wait_status(BASE1, old_id, {"solving"})
    time.sleep(2.0)
    assert not os.path.exists(gate_file(old_id))
    status, body = call(BASE1, "GET", f"/api/solve/{old_id}")
    print(f"   A 当前状态: {body['status']}（闸门未放行，线程仍存活）")
    assert body["status"] == "solving"

    # 3. 工程师调整配额后，在进程2重新提交求解 B
    step("3. 进程2受理新求解 B：A 必须立即 superseded")
    # 仍是按受试者整组可满足的新配额：glioma 不再占用 test（sub-003 并入 train）
    adjusted = {
        "glioma":     {"train": 11, "validation": 5, "test": 0},
        "meningioma": {"train": 8,  "validation": 4, "test": 3},
        "pituitary":  {"train": 6,  "validation": 4, "test": 2},
        "normal":     {"train": 9,  "validation": 4, "test": 4},
    }
    status, body = call(BASE2, "POST", "/api/solve",
                        {"quota": adjusted, "time_limit_seconds": 30})
    assert status == 200, body
    new_id = body["id"]
    print(f"   新任务 B id={new_id}")
    assert new_id != old_id
    for base in (BASE1, BASE2):
        status, body = call(base, "GET", f"/api/solve/{old_id}")
        print(f"   {base} 上看 A 的状态: {body['status']}")
        assert body["status"] == "superseded"

    # 4. 旧候选发布必须 409（两个进程都一样）
    step("4. 尝试发布旧候选 A（期望 409）")
    for base in (BASE1, BASE2):
        status, body = call(base, "POST", f"/api/solve/{old_id}/publish")
        print(f"   {base} POST publish A -> HTTP {status}: {body.get('detail')}")
        assert status == 409
    for sig in ("pause", "resume", "cancel"):
        status, _ = call(BASE2, "POST", f"/api/solve/{old_id}/{sig}")
        assert status == 409, (sig, status)
    print("   对 A 的 pause/resume/cancel 全部返回 409")

    # 5. B 同样停在闸门上；用进程1暂停进程2认领的任务（跨进程控制）
    step("5. 跨进程暂停 B（在进程1发信号，任务由进程2的 worker 执行）")
    time.sleep(2.0)
    status, body = call(BASE1, "GET", f"/api/solve/{new_id}")
    print(f"   暂停前 B 状态: {body['status']}")
    assert body["status"] == "solving"
    status, _ = call(BASE1, "POST", f"/api/solve/{new_id}/pause")
    assert status == 200
    paused = wait_status(BASE2, new_id, {"paused"})
    print(f"   B 已暂停: status={paused['status']} spent={paused['spent_seconds']}s")

    # 6. 此刻才放行旧求解 A：迟到成功不得复活 A，也不能消费 B 的暂停信号
    step("6. 放行旧求解 A 的闸门（迟到成功收尾）")
    release(old_id)
    time.sleep(1.5)
    status, old_body = call(BASE1, "GET", f"/api/solve/{old_id}")
    status, new_body = call(BASE2, "GET", f"/api/solve/{new_id}")
    print(f"   迟到成功后 A 状态: {old_body['status']}（必须仍是 superseded）")
    print(f"   迟到成功后 B 状态: {new_body['status']}（必须仍是 paused）")
    assert old_body["status"] == "superseded"
    assert new_body["status"] == "paused"
    status, _ = call(BASE1, "POST", f"/api/solve/{old_id}/publish")
    assert status == 409
    print("   A 依旧无法发布（409），B 的暂停信号未被旧线程清掉")

    # 7. 恢复 B 并放行 -> solved，然后跨进程发布
    step("7. 恢复 B、放行 B 的闸门并发布")
    status, _ = call(BASE2, "POST", f"/api/solve/{new_id}/resume")
    assert status == 200
    release(new_id)
    solved = wait_status(BASE1, new_id, {"solved"})
    print(f"   B 最终状态: {solved['status']}")
    status, pub = call(BASE1, "POST", f"/api/solve/{new_id}/publish")
    assert status == 200, pub
    print(f"   发布成功: published id={pub['id']}")
    status, body = call(BASE2, "GET", "/api/published")
    assert body["id"] == pub["id"]
    print(f"   进程2查询当前发布版本: id={body['id']}（跨进程一致）")

    # 8. 再演示取消：新任务 C 在闸门前被跨进程取消
    step("8. 新任务 C 的跨进程取消")
    status, body = call(BASE2, "POST", "/api/solve",
                        {"quota": adjusted, "time_limit_seconds": 30})
    c_id = body["id"]
    wait_status(BASE2, c_id, {"solving"})
    status, _ = call(BASE1, "POST", f"/api/solve/{c_id}/cancel")
    assert status == 200
    cancelled = wait_status(BASE1, c_id, {"cancelled"})
    print(f"   C 状态: {cancelled['status']}")
    status, body = call(BASE2, "GET", "/api/published")
    assert body["id"] == pub["id"]
    print(f"   已发布版本 id={pub['id']} 保持不变")

    print("\n全部断言通过：旧候选持续 superseded、发布 409、迟到收尾不复活、")
    print("新任务暂停/恢复/取消/发布均正常，控制信号严格按任务隔离。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
