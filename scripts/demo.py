"""端到端演示脚本：导入虚构数据 -> 冻结 -> 提交可暂停求解 ->
暂停/继续 -> 发布 -> 下载 CSV。

用法：
    python scripts/demo.py [base_url]
默认 base_url=http://127.0.0.1:8001（docker-compose 的 api-1 映射端口）。
脚本只依赖标准库，可直接在容器外运行。
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8001"

QUOTA = {
    "glioma":     {"train": 7, "validation": 5, "test": 4},
    "meningioma": {"train": 8, "validation": 4, "test": 3},
    "pituitary":  {"train": 6, "validation": 4, "test": 2},
    "normal":     {"train": 9, "validation": 4, "test": 4},
}


def call(method: str, path: str, body=None, raw: bytes | None = None,
         ctype: str = "application/json"):
    url = BASE + path
    data = None
    headers = {}
    if raw is not None:
        # 构造 multipart/form-data 请求体
        boundary = "----slice-demo-boundary-7f3a91"
        body_bytes = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="slices_seed.csv"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode() + raw + f"\r\n--{boundary}--\r\n".encode()
        data = body_bytes
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read()
            return resp.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def wait_status(sid: int, targets: set[str], timeout: float = 60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, d = call("GET", f"/api/solve/{sid}")
        if d.get("status") in targets:
            return d
        time.sleep(0.3)
    raise TimeoutError(f"task {sid} did not reach {targets}")


def main() -> int:
    print(f"== target: {BASE} ==")
    status, _ = call("GET", "/health")
    assert status == 200, "服务未就绪"

    fixture = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "data", "slices_seed.csv")
    with open(fixture, "rb") as fh:
        raw = fh.read()
    status, d = call("POST", "/api/samples/import-csv?mode=replace",
                     raw=raw, ctype="text/csv")
    print("1) 导入:", status, d)

    status, d = call("PUT", "/api/subjects/sub-003/freeze", {"group": "test"})
    print("2) 冻结 sub-003 -> test:", status, d)

    # min_hold 让求解保持存活，制造可观察的暂停窗口
    status, d = call("POST", "/api/solve", {
        "quota": QUOTA, "time_limit_seconds": 60, "min_hold_seconds": 30,
    })
    sid = d["id"]
    print(f"3) 提交求解 id={sid}（min_hold=30s），等待 worker 认领...")
    time.sleep(1.5)

    print("4) 暂停...")
    status, _ = call("POST", f"/api/solve/{sid}/pause")
    paused = wait_status(sid, {"paused"})
    print("   状态:", paused["status"], "已搜索秒数:", paused["spent_seconds"])

    print("5) 继续...")
    status, _ = call("POST", f"/api/solve/{sid}/resume")
    solved = wait_status(sid, {"solved", "infeasible"})
    print("   最终状态:", solved["status"], "分配:", solved.get("assignments"))
    assert solved["status"] == "solved"

    print("6) 发布...")
    status, pub = call("POST", f"/api/solve/{sid}/publish")
    print("   HTTP", status, "发布版本 id:", pub.get("id"),
          "分组计数:", pub.get("group_counts"))
    assert status == 200

    print("7) 下载 CSV ...")
    with urllib.request.urlopen(f"{BASE}/api/published/csv", timeout=30) as resp:
        text = resp.read().decode()
    rows = list(csv.DictReader(io.StringIO(text)))
    print(f"   CSV 数据行: {len(rows)}，表头: {text.splitlines()[0]}")
    print("演示完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
