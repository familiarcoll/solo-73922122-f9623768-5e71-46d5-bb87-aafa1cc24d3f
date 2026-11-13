"""重启持久化测试的子进程助手。

阶段 1（seed-and-publish）：全新进程启动应用 -> 导入 -> 求解 -> 发布，打印发布 id 后退出。
阶段 2（verify <id>）：另一个全新进程打开同一个数据库，验证发布版本与 CSV 仍可查询。
"""
import sys
import time

from fastapi.testclient import TestClient

from app import database
from app.manager import manager
from app.main import app

QUOTA = {
    "glioma":     {"train": 7, "validation": 5, "test": 4},
    "meningioma": {"train": 8, "validation": 4, "test": 3},
    "pituitary":  {"train": 6, "validation": 4, "test": 2},
    "normal":     {"train": 9, "validation": 4, "test": 4},
}

PLAN = [
    ("sub-001", "glioma", 7), ("sub-002", "glioma", 5), ("sub-003", "glioma", 4),
    ("sub-004", "meningioma", 8), ("sub-005", "meningioma", 4),
    ("sub-006", "meningioma", 3),
    ("sub-007", "pituitary", 6), ("sub-008", "pituitary", 4),
    ("sub-009", "pituitary", 2),
    ("sub-010", "normal", 9), ("sub-011", "normal", 4), ("sub-012", "normal", 4),
]


def seed_rows():
    rows = []
    for sid, disease, n in PLAN:
        for i in range(1, n + 1):
            rows.append({"sample_id": f"{sid}_slice-{i:03d}",
                         "subject_id": sid, "disease": disease})
    return rows


def wait_solved(client, solution_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/solve/{solution_id}").json()
        if r["status"] in ("solved", "infeasible", "failed", "cancelled"):
            return r
        time.sleep(0.1)
    raise RuntimeError("solve timed out")


def main() -> int:
    stage = sys.argv[1]
    database.init_db()
    manager.start()
    try:
        with TestClient(app) as client:
            if stage == "seed-and-publish":
                r = client.post("/api/samples/import",
                                json={"samples": seed_rows()})
                assert r.status_code == 200, r.text
                r = client.post("/api/solve", json={"quota": QUOTA})
                sid = r.json()["id"]
                result = wait_solved(client, sid)
                assert result["status"] == "solved", result
                r = client.post(f"/api/solve/{sid}/publish")
                assert r.status_code == 200, r.text
                print(r.json()["id"])
                return 0

            if stage == "verify":
                published_id = int(sys.argv[2])
                r = client.get(f"/api/published?published_id={published_id}")
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["group_counts"] == QUOTA
                assert len(body["input_snapshot"]["samples"]) == 60
                csv_r = client.get(
                    f"/api/published/csv?published_id={published_id}")
                assert csv_r.status_code == 200
                assert len(csv_r.text.strip().splitlines()) == 61
                print("VERIFY-OK")
                return 0

            print(f"unknown stage {stage}", file=sys.stderr)
            return 2
    finally:
        manager.stop()


if __name__ == "__main__":
    sys.exit(main())
