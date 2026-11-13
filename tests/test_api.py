"""端到端 pytest：覆盖需求中的全部关键语义。

运行：
    pytest -q
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient


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

QUOTA = {
    "glioma":     {"train": 7, "validation": 5, "test": 4},
    "meningioma": {"train": 8, "validation": 4, "test": 3},
    "pituitary":  {"train": 6, "validation": 4, "test": 2},
    "normal":     {"train": 9, "validation": 4, "test": 4},
}


def _import(client, rows=None, mode="replace"):
    rows = rows or SEED_ROWS
    return client.post(f"/api/samples/import?mode={mode}", json={"samples": rows})


def _wait_solved(client, solution_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/solve/{solution_id}").json()
        if r["status"] in ("solved", "infeasible", "failed", "cancelled"):
            return r
        time.sleep(0.1)
    raise AssertionError("solve timed out")


def _wait_claimed(client, solution_id, timeout=5.0):
    """等待监督线程认领任务（heartbeat 出现），确保信号可被 worker 观察。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/solve/{solution_id}").json()
        if r.get("heartbeat") or r["status"] in (
            "solved", "infeasible", "failed", "cancelled", "paused"
        ):
            return r
        time.sleep(0.05)
    raise AssertionError("task was not claimed in time")


def test_import_and_caps(client):
    r = _import(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == 60
    assert body["data_version"] == 2  # 初始为 1，导入后自增

    r = client.get("/api/samples").json()
    assert r["total"] == 60
    assert r["subject_count"] == 12
    assert sorted(r["diseases"]) == ["glioma", "meningioma", "normal", "pituitary"]

    # 上限校验
    too_many = [
        {"sample_id": f"s{i}", "subject_id": f"u{i}", "disease": "d"}
        for i in range(201)
    ]
    r = _import(client, too_many)
    assert r.status_code == 422

    # 重复 sample_id（append 模式）
    r = client.post("/api/samples/import?mode=append",
                    json={"samples": [SEED_ROWS[0]]})
    assert r.status_code == 409


def test_quota_validation(client):
    _import(client)
    # 配额之和不等于样本总数
    bad = {k: dict(v, train=v["train"] + 1) for k, v in QUOTA.items()}
    r = client.post("/api/solve", json={"quota": bad})
    assert r.status_code == 422
    # 未知病种
    r = client.post("/api/solve", json={"quota": {**QUOTA, "ghost": QUOTA["glioma"]}})
    assert r.status_code == 422
    # 缺病种
    r = client.post("/api/solve", json={"quota": {"glioma": QUOTA["glioma"]}})
    assert r.status_code == 422


def test_full_solve_publish_and_exact_constraints(client):
    _import(client)
    # 冻结 sub-003（glioma, 4 切片）到 test
    r = client.put("/api/subjects/sub-003/freeze", json={"group": "test"})
    assert r.status_code == 200

    r = client.post("/api/solve", json={"quota": QUOTA})
    assert r.status_code == 200
    sid = r.json()["id"]
    result = _wait_solved(client, sid)
    assert result["status"] == "solved", result

    # 冻结必须被尊重
    assert result["assignments"]["sub-003"] == "test"

    # 用快照核对：受试者全部样本同组 + 每病种精确配额
    snap = result["input_snapshot"]
    assignments = result["assignments"]
    subj_groups = {}
    counts = {d: {g: 0 for g in ("train", "validation", "test")}
              for d in QUOTA}
    for smp in snap["samples"]:
        g = assignments[smp["subject_id"]]
        assert subj_groups.setdefault(smp["subject_id"], g) == g  # 同受试者同组
        counts[smp["disease"]][g] += 1
    assert counts == QUOTA

    # 发布
    r = client.post(f"/api/solve/{sid}/publish")
    assert r.status_code == 200, r.text
    pub = r.json()
    assert pub["kind"] == "published"
    assert pub["group_counts"] == QUOTA

    # 查询与 CSV 对应同一版本
    r = client.get("/api/published").json()
    assert r["id"] == pub["id"]
    csv_r = client.get("/api/published/csv")
    assert csv_r.status_code == 200
    csv_text = csv_r.text
    assert "sample_id,subject_id,disease,group,solution_id,input_version" in csv_text
    assert f",{pub['id']},{pub['input_version']}" in csv_text
    # 行数 = 60 数据行 + 1 表头
    assert len(csv_text.strip().splitlines()) == 61


def test_infeasible_does_not_change_published(client):
    _import(client)
    r = client.post("/api/solve", json={"quota": QUOTA})
    good = _wait_solved(client, r.json()["id"])
    assert good["status"] == "solved"
    client.post(f"/api/solve/{good['id']}/publish")
    published_before = client.get("/api/published").json()["id"]

    # 构造无解：冻结 sub-007（pituitary, 6 切片）到 test，
    # 但 pituitary 的 test 配额为 0，硬约束互相矛盾
    client.put("/api/subjects/sub-007/freeze", json={"group": "test"})
    impossible = {
        "glioma":     {"train": 8,  "validation": 4, "test": 4},
        "meningioma": {"train": 8,  "validation": 4, "test": 3},
        "pituitary":  {"train": 8,  "validation": 4, "test": 0},
        "normal":     {"train": 9,  "validation": 4, "test": 4},
    }
    r = client.post("/api/solve", json={"quota": impossible,
                                        "time_limit_seconds": 5})
    result = _wait_solved(client, r.json()["id"], timeout=15)
    assert result["status"] == "infeasible"

    # 已发布结果不变
    still = client.get("/api/published").json()
    assert still["id"] == published_before
    # 无解候选不可发布
    r = client.post(f"/api/solve/{result['id']}/publish")
    assert r.status_code == 409


def test_stale_candidate_publish_409_even_after_revert(client):
    _import(client)
    r = client.post("/api/solve", json={"quota": QUOTA})
    sid = r.json()["id"]
    result = _wait_solved(client, sid)
    assert result["status"] == "solved"
    base_version = result["input_version"]

    # 求解后修改一个样本的病种
    target = SEED_ROWS[0]["sample_id"]
    r = client.patch(f"/api/samples/{target}", json={"disease": "normal"})
    assert r.status_code == 200
    assert r.json()["data_version"] == base_version + 1

    cand = client.get(f"/api/solve/{sid}").json()
    assert cand["stale"] is True
    r = client.post(f"/api/solve/{sid}/publish")
    assert r.status_code == 409

    # 改回原值：版本号继续增加，候选仍然失效
    r = client.patch(f"/api/samples/{target}", json={"disease": "glioma"})
    assert r.status_code == 200
    r = client.post(f"/api/solve/{sid}/publish")
    assert r.status_code == 409
    assert client.get(f"/api/solve/{sid}").json()["stale"] is True

    # 冻结修改同样使候选失效
    r2 = client.post("/api/solve", json={"quota": QUOTA})
    sid2 = r2.json()["id"]
    _wait_solved(client, sid2)
    client.put("/api/subjects/sub-001/freeze", json={"group": "train"})
    r = client.post(f"/api/solve/{sid2}/publish")
    assert r.status_code == 409


def test_pause_resume_cancel_with_hold(client):
    _import(client)
    # min_hold 让求解保持运行，便于观察暂停
    r = client.post("/api/solve", json={
        "quota": QUOTA, "time_limit_seconds": 30, "min_hold_seconds": 20,
    })
    sid = r.json()["id"]
    claimed = _wait_claimed(client, sid)
    assert claimed["status"] == "solving"
    # 再等一个求解分片，确保 worker 已实际消耗搜索时间后再发暂停
    time.sleep(0.4)
    assert client.get(f"/api/solve/{sid}").json()["status"] == "solving"

    # 暂停
    r = client.post(f"/api/solve/{sid}/pause")
    assert r.status_code == 200
    deadline = time.time() + 5
    while time.time() < deadline:
        if client.get(f"/api/solve/{sid}").json()["status"] == "paused":
            break
        time.sleep(0.1)
    paused = client.get(f"/api/solve/{sid}").json()
    assert paused["status"] == "paused"
    spent = paused["spent_seconds"]
    assert spent >= 0

    # 暂停状态下再暂停 -> 409
    assert client.post(f"/api/solve/{sid}/pause").status_code == 409

    # 继续，最终求解成功，且累计耗时大于暂停前
    r = client.post(f"/api/solve/{sid}/resume")
    assert r.status_code == 200
    result = _wait_solved(client, sid, timeout=30)
    assert result["status"] == "solved"
    assert result["spent_seconds"] >= spent

    # 取消：新任务后取消
    r = client.post("/api/solve", json={
        "quota": QUOTA, "time_limit_seconds": 30, "min_hold_seconds": 20,
    })
    sid2 = r.json()["id"]
    _wait_claimed(client, sid2)
    r = client.post(f"/api/solve/{sid2}/cancel")
    assert r.status_code == 200
    result = _wait_solved(client, sid2, timeout=10)
    assert result["status"] == "cancelled"


def test_new_solve_supersedes_paused_old_task(client):
    _import(client)
    r = client.post("/api/solve", json={
        "quota": QUOTA, "time_limit_seconds": 30, "min_hold_seconds": 25,
    })
    old_id = r.json()["id"]
    _wait_claimed(client, old_id)
    client.post(f"/api/solve/{old_id}/pause")
    deadline = time.time() + 5
    while time.time() < deadline:
        if client.get(f"/api/solve/{old_id}").json()["status"] == "paused":
            break
        time.sleep(0.1)
    assert client.get(f"/api/solve/{old_id}").json()["status"] == "paused"

    # 提交新求解：旧暂停任务应立即被标记 superseded，且不能再接受信号
    r = client.post("/api/solve", json={"quota": QUOTA})
    new_id = r.json()["id"]
    assert new_id != old_id
    assert client.get(f"/api/solve/{old_id}").json()["status"] == "superseded"
    assert client.post(f"/api/solve/{old_id}/resume").status_code == 409

    # 新任务正常求解，旧任务不复活
    result = _wait_solved(client, new_id)
    assert result["status"] == "solved"
    assert client.get(f"/api/solve/{old_id}").json()["status"] == "superseded"


def test_solve_runs_without_write_lock_during_mutation(client):
    _import(client)
    r = client.post("/api/solve", json={
        "quota": QUOTA, "time_limit_seconds": 20, "min_hold_seconds": 15,
    })
    sid = r.json()["id"]
    _wait_claimed(client, sid)
    # 求解进行中：补样、改病种、改冻结都应立即成功（不被写锁阻塞）
    extra = {"sample_id": "sub-013_slice-001", "subject_id": "sub-013",
             "disease": "normal"}
    r = client.post("/api/samples/import?mode=append", json={"samples": [extra]})
    assert r.status_code == 200
    r = client.patch("/api/samples/sub-013_slice-001", json={"disease": "glioma"})
    assert r.status_code == 200
    r = client.put("/api/subjects/sub-013/freeze", json={"group": "train"})
    assert r.status_code == 200
    # 旧候选（求解中的那个）已经因为版本推进而失效
    assert client.get(f"/api/solve/{sid}").json()["stale"] is True
    client.post(f"/api/solve/{sid}/cancel")


def test_csv_import_endpoint(client):
    buf = io.StringIO()
    import csv as _csv
    w = _csv.DictWriter(buf, fieldnames=["sample_id", "subject_id", "disease"])
    w.writeheader()
    for row in SEED_ROWS[:5]:
        w.writerow(row)
    r = client.post(
        "/api/samples/import-csv?mode=replace",
        files={"file": ("slices.csv", buf.getvalue().encode(), "text/csv")},
    )
    assert r.status_code == 200
    assert r.json()["imported"] == 5


def test_published_versions_immutable_and_history(client):
    _import(client)
    r = client.post("/api/solve", json={"quota": QUOTA})
    sid = _wait_solved(client, r.json()["id"])["id"]
    client.post(f"/api/solve/{sid}/publish")
    first = client.get("/api/published").json()

    # 数据变化后重新求解并发布第二个版本：删除 sub-001 的全部 7 个切片，
    # glioma 从 16 降为 9（受试者 sub-002=5、sub-003=4 不变，配额仍可按受试者整组满足）
    sub001_samples = [r["sample_id"] for r in SEED_ROWS
                      if r["subject_id"] == "sub-001"]
    assert len(sub001_samples) == 7
    for sample_id in sub001_samples:
        r = client.delete(f"/api/samples/{sample_id}")
        assert r.status_code == 200
    new_quota = {
        "glioma":     {"train": 5, "validation": 4, "test": 0},
        "meningioma": {"train": 8, "validation": 4, "test": 3},
        "pituitary":  {"train": 6, "validation": 4, "test": 2},
        "normal":     {"train": 9, "validation": 4, "test": 4},
    }
    assert sum(new_quota["glioma"].values()) == 9
    assert sum(new_quota["normal"].values()) == 17
    r = client.post("/api/solve", json={"quota": new_quota})
    sid2 = _wait_solved(client, r.json()["id"])["id"]
    client.post(f"/api/solve/{sid2}/publish")
    second = client.get("/api/published").json()
    assert second["id"] != first["id"]

    # 历史版本仍可查询与下载，内容保持发布时快照不变
    old = client.get(f"/api/published?published_id={first['id']}").json()
    assert old["input_snapshot"] == first["input_snapshot"]
    old_csv = client.get(f"/api/published/csv?published_id={first['id']}")
    assert old_csv.status_code == 200
    history = client.get("/api/published/history").json()["versions"]
    assert {v["id"] for v in history} == {first["id"], second["id"]}


def test_persistence_across_restart(tmp_path):
    """用两个独立子进程模拟服务重启，验证数据与发布版本持久化。"""
    db_file = tmp_path / "persist.db"
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**os.environ, "SLICE_DB_PATH": str(db_file),
           "PYTHONPATH": repo_root}
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "_restart_helper.py")

    first = subprocess.run(
        [sys.executable, helper, "seed-and-publish"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert first.returncode == 0, first.stderr
    published_id = int(first.stdout.strip().splitlines()[-1])

    second = subprocess.run(
        [sys.executable, helper, "verify", str(published_id)],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert second.returncode == 0, second.stderr
    assert "VERIFY-OK" in second.stdout
