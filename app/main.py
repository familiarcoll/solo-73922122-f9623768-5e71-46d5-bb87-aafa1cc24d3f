"""FastAPI 应用：切片分组服务的全部 HTTP 接口。

关键语义：
- 任何数据修改都推动 data_version 自增（单调不回退），
  候选记录保存求解时刻的完整输入快照与版本号；
- 发布时若候选版本与当前版本不一致 -> 409，改回原值也无法恢复（版本号不回退）；
- 无解候选不会触碰已发布版本；发布版本不可变，查询与 CSV 下载均基于发布时的快照；
- 求解在后台线程进行，CP-SAT 计算期间不持有数据库写锁，期间可正常修改数据；
- 两个 API 进程共享同一个 SQLite 文件（WAL 模式），任务可跨进程认领与控制。
"""
from __future__ import annotations

import csv
import io
import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, Query, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from . import database as db
from .config import GROUPS, MAX_DISEASES, MAX_SAMPLES, MAX_SUBJECTS
from .errors import ServiceError
from .manager import manager
from .schemas import FreezeRequest, SamplePatch, SolveRequest


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    manager.start()
    try:
        yield
    finally:
        manager.stop()


app = FastAPI(
    title="医学影像切片分组服务",
    description=(
        "按受试者防泄漏地把标注切片划分为 train/validation/test。\n\n"
        "- 同一受试者的全部样本只能进入同一个集合\n"
        "- 各病种在三个集合中的样本数精确满足请求配额\n"
        "- 可冻结指定受试者的归组\n"
        "- 候选基于求解时刻的完整输入快照；数据一旦修改，旧候选发布即返回 409\n"
        "- 重新求解会把旧候选原子置为 superseded 且不可被旧 worker 的迟到收尾复活；\n"
        "  控制信号按任务隔离，旧任务无法消费新任务的暂停/取消信号"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(ServiceError)
def _service_error_handler(_request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# ---------------------------------------------------------------------------
# 样本导入与维护
# ---------------------------------------------------------------------------

def _validate_rows(rows: list[dict[str, str]]) -> list[tuple[str, str, str]]:
    if not rows:
        raise ServiceError(422, "CSV/JSON 中没有任何数据行")
    if len(rows) > MAX_SAMPLES:
        raise ServiceError(422, f"单次导入样本数超过上限 {MAX_SAMPLES}")
    cleaned: list[tuple[str, str, str]] = []
    sample_ids: set[str] = set()
    subjects: set[str] = set()
    diseases: set[str] = set()
    for i, row in enumerate(rows, start=2):
        try:
            sid = str(row["sample_id"]).strip()
            sub = str(row["subject_id"]).strip()
            dis = str(row["disease"]).strip()
        except KeyError as exc:
            raise ServiceError(422, f"第 {i} 行缺少列: {exc.args[0]}")
        if not sid or not sub or not dis:
            raise ServiceError(422, f"第 {i} 行存在空字段")
        if sid in sample_ids:
            raise ServiceError(422, f"文件内 sample_id 重复: {sid}（第 {i} 行）")
        sample_ids.add(sid)
        subjects.add(sub)
        diseases.add(dis)
        cleaned.append((sid, sub, dis))
    if len(subjects) > MAX_SUBJECTS:
        raise ServiceError(422, f"受试者数超过上限 {MAX_SUBJECTS}")
    if len(diseases) > MAX_DISEASES:
        raise ServiceError(422, f"病种数超过上限 {MAX_DISEASES}")
    return cleaned


@app.post("/api/samples/import", tags=["样本管理"])
def import_samples_json(
    payload: dict[str, Any],
    mode: str = Query("replace", pattern="^(replace|append)$"),
) -> dict[str, Any]:
    """通过 JSON 导入样本：{"samples": [{sample_id, subject_id, disease}, ...]}"""
    rows = payload.get("samples")
    if not isinstance(rows, list):
        raise ServiceError(422, "请求体必须包含 samples 数组")
    cleaned = _validate_rows(rows)
    result = db.import_samples(cleaned, mode)
    return result


@app.post("/api/samples/import-csv", tags=["样本管理"])
async def import_samples_csv(
    file: UploadFile = File(..., description="CSV 文件，表头: sample_id,subject_id,disease"),
    mode: str = Query("replace", pattern="^(replace|append)$"),
) -> dict[str, Any]:
    """通过 CSV 文件导入样本。"""
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ServiceError(422, "CSV 文件必须为 UTF-8 编码")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or not {
        "sample_id", "subject_id", "disease"
    }.issubset({f.strip() for f in reader.fieldnames}):
        raise ServiceError(
            422, "CSV 表头必须为 sample_id,subject_id,disease"
        )
    rows = list(reader)
    cleaned = _validate_rows(rows)
    return db.import_samples(cleaned, mode)


@app.get("/api/samples", tags=["样本管理"])
def get_samples() -> dict[str, Any]:
    samples = db.list_samples()
    diseases = sorted({s["disease"] for s in samples})
    subjects = sorted({s["subject_id"] for s in samples})
    counts = {d: 0 for d in diseases}
    for s in samples:
        counts[s["disease"]] += 1
    return {
        "data_version": _version(),
        "total": len(samples),
        "subject_count": len(subjects),
        "diseases": diseases,
        "disease_counts": counts,
        "samples": samples,
    }


@app.patch("/api/samples/{sample_id}", tags=["样本管理"])
def patch_sample(sample_id: str, patch: SamplePatch) -> dict[str, Any]:
    """修改样本的病种或所属受试者（会使旧候选失效）。"""
    return db.update_sample(sample_id, patch.disease, patch.subject_id)


@app.delete("/api/samples/{sample_id}", tags=["样本管理"])
def remove_sample(sample_id: str) -> dict[str, Any]:
    return db.delete_sample(sample_id)


# ---------------------------------------------------------------------------
# 受试者与冻结归组
# ---------------------------------------------------------------------------

@app.get("/api/subjects", tags=["受试者管理"])
def get_subjects() -> dict[str, Any]:
    subjects = db.list_subjects()
    return {"data_version": _version(), "total": len(subjects), "subjects": subjects}


@app.put("/api/subjects/{subject_id}/freeze", tags=["受试者管理"])
def freeze_subject(subject_id: str, req: FreezeRequest) -> dict[str, Any]:
    """冻结受试者：其全部样本在求解时只能进入指定集合。"""
    return db.set_freeze(subject_id, req.group)


@app.delete("/api/subjects/{subject_id}/freeze", tags=["受试者管理"])
def unfreeze_subject(subject_id: str) -> dict[str, Any]:
    return db.clear_freeze(subject_id)


# ---------------------------------------------------------------------------
# 求解任务
# ---------------------------------------------------------------------------

@app.post("/api/solve", tags=["求解与发布"])
def create_solve(req: SolveRequest) -> dict[str, Any]:
    """提交分组求解。立即返回任务信息，求解在后台进行。

    - quota: 每个病种在 train/validation/test 上的精确样本数，三者之和须等于该病种样本总数；
    - min_hold_seconds: 演示用，使求解至少保持运行该时长，方便观察暂停/继续/取消。
    """
    return manager.submit(req.quota, req.time_limit_seconds, req.min_hold_seconds)


@app.get("/api/solve/{solution_id}", tags=["求解与发布"])
def solve_status(solution_id: int) -> dict[str, Any]:
    return manager.get_status(solution_id)


@app.get("/api/candidate", tags=["求解与发布"])
def latest_candidate() -> dict[str, Any]:
    conn = db.get_conn()
    try:
        row = db.get_latest(conn, "candidate")
        if row is None:
            raise ServiceError(404, "当前没有任何候选")
        return manager._decorate(conn, db.row_to_dict(row))
    finally:
        conn.close()


@app.post("/api/solve/{solution_id}/pause", tags=["求解与发布"])
def pause_solve(solution_id: int) -> dict[str, Any]:
    return manager.send_signal(solution_id, "pause")


@app.post("/api/solve/{solution_id}/resume", tags=["求解与发布"])
def resume_solve(solution_id: int) -> dict[str, Any]:
    return manager.send_signal(solution_id, "resume")


@app.post("/api/solve/{solution_id}/cancel", tags=["求解与发布"])
def cancel_solve(solution_id: int) -> dict[str, Any]:
    return manager.send_signal(solution_id, "cancel")


# ---------------------------------------------------------------------------
# 发布（不可变版本）
# ---------------------------------------------------------------------------

@app.post("/api/solve/{solution_id}/publish", tags=["求解与发布"])
def publish(solution_id: int) -> dict[str, Any]:
    """发布候选。

    - 候选必须已求解成功；
    - 已被更新求解取代的候选（superseded）返回 409，且旧 worker 迟到的
      成功/暂停/取消/异常收尾都不可能把它改回 solved；
    - 候选保存的输入版本必须等于当前数据版本，否则 409
      （求解后任何修改都会导致版本不符，把值改回去也不会回退版本号）；
    - 无解（infeasible）的候选无法发布，已发布结果保持不变。
    """
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = db.get_solution(conn, solution_id)
        if row is None or row["kind"] != "candidate":
            raise ServiceError(404, f"候选不存在: {solution_id}")
        if row["status"] != "solved":
            if row["status"] == "superseded":
                raise ServiceError(
                    409,
                    "候选已被更新的求解任务取代（superseded），不能发布；"
                    "请发布最新候选",
                )
            raise ServiceError(
                409, f"候选状态为 {row['status']}，只有 solved 的候选可以发布"
            )
        version = db.current_version(conn)
        if row["input_version"] != version:
            raise ServiceError(
                409,
                f"候选基于数据版本 {row['input_version']}，当前版本为 {version}；"
                "求解后数据已被修改，候选已失效，请重新求解（即使把值改回原值也无法恢复）",
            )
        # 旧发布版本标记 superseded（行本身保留，历史不可变、仍可按 id 查询/下载）
        conn.execute(
            "UPDATE solutions SET status = 'superseded' "
            "WHERE kind = 'published' AND status != 'superseded'"
        )
        data = db.row_to_dict(row)
        cur = conn.execute(
            """INSERT INTO solutions
               (kind, status, input_version, quota_request, input_snapshot,
                assignments, error_message, spent_seconds, published_at)
               VALUES ('published', 'solved', ?, ?, ?, ?, NULL, ?,
                       strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))""",
            (
                data["input_version"],
                json.dumps(data["quota_request"], ensure_ascii=False),
                json.dumps(data["input_snapshot"], ensure_ascii=False),
                json.dumps(data["assignments"], ensure_ascii=False),
                data["spent_seconds"],
            ),
        )
        published_id = cur.lastrowid
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return get_published(published_id)


@app.get("/api/published", tags=["发布版本"])
def get_published(published_id: int | None = None) -> dict[str, Any]:
    """查询发布版本。不带 id 时返回当前发布版本；带 id 时可查历史版本。

    返回内容包含发布时保存的完整输入快照与分配，与当前库数据无关。
    """
    conn = db.get_conn()
    try:
        if published_id is None:
            row = conn.execute(
                "SELECT * FROM solutions WHERE kind = 'published' "
                "AND status = 'solved' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM solutions WHERE id = ? AND kind = 'published'",
                (published_id,),
            ).fetchone()
        if row is None:
            raise ServiceError(404, "没有符合条件的发布版本")
        data = db.row_to_dict(row)
        data["group_counts"] = _group_counts(data)
        return data
    finally:
        conn.close()


@app.get("/api/published/history", tags=["发布版本"])
def publish_history() -> dict[str, Any]:
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT id, input_version, status, published_at, quota_request "
            "FROM solutions WHERE kind = 'published' ORDER BY id DESC"
        ).fetchall()
        return {
            "versions": [
                {
                    "id": r["id"],
                    "input_version": r["input_version"],
                    "status": r["status"],
                    "published_at": r["published_at"],
                    "quota_request": json.loads(r["quota_request"]),
                }
                for r in rows
            ]
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CSV 下载（与查询对应同一个完整不可变版本）
# ---------------------------------------------------------------------------

def _group_counts(data: dict[str, Any]) -> dict[str, dict[str, int]]:
    assignments = data["assignments"] or {}
    counts: dict[str, dict[str, int]] = {}
    for smp in data["input_snapshot"]["samples"]:
        g = assignments.get(smp["subject_id"])
        counts.setdefault(smp["disease"], {g: 0 for g in GROUPS})
        counts[smp["disease"]][g] = counts[smp["disease"]].get(g, 0) + 1
    return counts


def _build_csv(data: dict[str, Any]) -> str:
    assignments = data["assignments"] or {}
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "sample_id", "subject_id", "disease", "group",
        "solution_id", "input_version",
    ])
    for smp in sorted(data["input_snapshot"]["samples"], key=lambda s: s["sample_id"]):
        writer.writerow([
            smp["sample_id"], smp["subject_id"], smp["disease"],
            assignments.get(smp["subject_id"], ""),
            data["id"], data["input_version"],
        ])
    return buf.getvalue()


@app.get("/api/published/csv", tags=["发布版本"])
def download_published_csv(published_id: int | None = None) -> StreamingResponse:
    """下载当前（或指定历史）发布版本的 CSV。数据取自发布时的不可变快照。"""
    data = get_published(published_id)
    csv_text = _build_csv(data)
    filename = f"published_solution_{data['id']}_v{data['input_version']}.csv"
    return StreamingResponse(
        io.BytesIO(csv_text.encode("utf-8")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/candidate/csv", tags=["求解与发布"])
def download_candidate_csv() -> StreamingResponse:
    """下载当前最新候选的 CSV（同样基于候选保存的完整快照）。"""
    conn = db.get_conn()
    try:
        row = db.get_latest(conn, "candidate")
        if row is None:
            raise ServiceError(404, "当前没有任何候选")
        data = db.row_to_dict(row)
    finally:
        conn.close()
    if data["status"] != "solved":
        raise ServiceError(409, f"候选状态为 {data['status']}，尚无分配结果可下载")
    csv_text = _build_csv(data)
    filename = f"candidate_solution_{data['id']}_v{data['input_version']}.csv"
    return StreamingResponse(
        io.BytesIO(csv_text.encode("utf-8")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# 健康检查与元信息
# ---------------------------------------------------------------------------

def _version() -> int:
    conn = db.get_conn()
    try:
        return db.current_version(conn)
    finally:
        conn.close()


@app.get("/api/meta", tags=["系统"])
def meta() -> dict[str, Any]:
    conn = db.get_conn()
    try:
        version = db.current_version(conn)
        n = conn.execute("SELECT COUNT(*) c FROM solutions").fetchone()["c"]
        return {
            "data_version": version,
            "limits": {
                "max_samples": MAX_SAMPLES,
                "max_subjects": MAX_SUBJECTS,
                "max_diseases": MAX_DISEASES,
            },
            "solution_rows": n,
            "worker": manager.worker_id,
        }
    finally:
        conn.close()


@app.get("/health", tags=["系统"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/shutdown", tags=["系统"], include_in_schema=False)
def shutdown() -> dict[str, str]:
    """测试专用优雅关停：仅在显式设置 SLICE_ENABLE_SHUTDOWN=1 时可用。

    生产/Compose 部署不设置该变量，此端点返回 404 等价的 403，
    避免任何外部请求关停服务。
    """
    import os
    import threading

    if os.environ.get("SLICE_ENABLE_SHUTDOWN") != "1":
        raise ServiceError(403, "shutdown endpoint disabled")
    manager.stop()

    def _exit() -> None:
        import time as _time
        _time.sleep(0.1)
        os._exit(0)

    threading.Thread(target=_exit, daemon=True).start()
    return {"status": "shutting down"}
