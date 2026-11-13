"""SQLite 持久化层。

设计要点：
- 启用 WAL，两个 API 进程可同时打开同一个数据库文件，读写互不阻塞；
- 所有事务都设置 busy_timeout，写冲突时自动等待而不是立刻报 SQLITE_BUSY；
- 任何有效修改（导入、改病种、改冻结、补样、删除）都会使 metadata.data_version 自增，
  该版本号单调递增、永不回退，因此"改回原值"也无法恢复旧候选；
- 求解过程不持有任何数据库写锁，只在开始时做一次短暂读快照；
- solve_controls 以 solution_id 为主键、每个候选一行，控制信号按任务隔离；
- guarded_transition 为 worker 的所有状态写入加上 worker 归属与活跃状态条件，
  保证被新求解取代（superseded）或被接管的任务不会被旧线程的迟到收尾复活。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Iterable

from . import config
from .config import GROUPS, MAX_DISEASES, MAX_SAMPLES, MAX_SUBJECTS
from .errors import ServiceError

_init_lock = threading.Lock()
_initialized = False

# 哨兵值：作为字段值传入时，SQL 里直接展开为当前 UTC 时间戳表达式（不绑定参数）。
SQLITE_NOW = object()


def _set_clause(field: str) -> str:
    return (
        f"{field} = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        if field == "heartbeat"
        else f"{field} = ?"
    )


def reset_for_test() -> None:
    """测试夹具使用：重置初始化标记，使下一次 init_db 重建新库的 schema。"""
    global _initialized
    _initialized = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS samples (
    sample_id   TEXT PRIMARY KEY,
    subject_id  TEXT NOT NULL,
    disease     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_subject ON samples(subject_id);
CREATE INDEX IF NOT EXISTS idx_samples_disease ON samples(disease);

-- 受试者冻结归组。只有被显式冻结的受试者才会出现在这里。
CREATE TABLE IF NOT EXISTS subject_freeze (
    subject_id TEXT PRIMARY KEY,
    group_name TEXT NOT NULL CHECK (group_name IN ('train', 'validation', 'test'))
);

-- 候选与发布版本。同一时刻至多一个 candidate、至多一个 published。
-- input_snapshot 保存求解时刻的完整输入（全部样本、冻结关系、配额请求）。
CREATE TABLE IF NOT EXISTS solutions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL CHECK (kind IN ('candidate', 'published')),
    status          TEXT NOT NULL CHECK (status IN ('solving', 'solved', 'infeasible', 'paused', 'cancelled', 'failed', 'superseded')),
    input_version   INTEGER NOT NULL,
    quota_request   TEXT NOT NULL,
    input_snapshot  TEXT NOT NULL,
    assignments     TEXT,
    error_message   TEXT,
    spent_seconds   REAL NOT NULL DEFAULT 0,
    worker_id       TEXT,
    heartbeat       TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    published_at    TEXT
);

-- 求解过程的跨进程控制通道：另一个 API 进程也能写入信号。
-- 每个候选任务一行（以 solution_id 为主键），任务之间的信号严格隔离：
-- 旧任务的 worker 收尾时只能清除自己那一行，不可能消费新任务的暂停/取消信号。
CREATE TABLE IF NOT EXISTS solve_controls (
    solution_id INTEGER PRIMARY KEY,
    signal      TEXT NOT NULL CHECK (signal IN ('pause', 'resume', 'cancel'))
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(
        config.get_db_path(),
        timeout=30.0,
        isolation_level=None,  # 显式事务，自动提交关闭
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _migrate_controls_table(conn: sqlite3.Connection) -> None:
    """把旧版单行控制表（id=1）迁移为按 solution_id 隔离的新表。"""
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(solve_controls)").fetchall()
    }
    if not cols or "id" not in cols:
        return  # 全新库（SCHEMA 已建新表）或已是新结构
    conn.execute("ALTER TABLE solve_controls RENAME TO solve_controls_legacy")
    conn.execute(
        """CREATE TABLE solve_controls (
               solution_id INTEGER PRIMARY KEY,
               signal      TEXT NOT NULL CHECK (signal IN ('pause', 'resume', 'cancel'))
           )"""
    )
    conn.execute(
        """INSERT INTO solve_controls (solution_id, signal)
           SELECT solution_id, signal FROM solve_controls_legacy
            WHERE solution_id IS NOT NULL AND signal IS NOT NULL"""
    )
    conn.execute("DROP TABLE solve_controls_legacy")


def init_db() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        conn = get_conn()
        try:
            conn.executescript(SCHEMA)
            _migrate_controls_table(conn)
            conn.execute(
                "INSERT OR IGNORE INTO metadata (id, data_version) VALUES (1, 1)"
            )
        finally:
            conn.close()
        _initialized = True


def _rollback_safely(conn: sqlite3.Connection) -> None:
    """回滚；若事务已经结束（例如异常发生在 BEGIN 之前）则忽略。"""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass


# ---------------------------------------------------------------------------
# 版本号与失效控制
# ---------------------------------------------------------------------------

def bump_version(conn: sqlite3.Connection) -> int:
    """使当前数据版本自增，返回新版本号。必须在写事务内调用。"""
    cur = conn.execute(
        "UPDATE metadata SET data_version = data_version + 1 WHERE id = 1"
    )
    if cur.rowcount == 0:
        raise ServiceError(500, "metadata row missing")
    row = conn.execute("SELECT data_version FROM metadata WHERE id = 1").fetchone()
    return row["data_version"]


def current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT data_version FROM metadata WHERE id = 1").fetchone()[
        "data_version"
    ]


# ---------------------------------------------------------------------------
# 数据修改（均在事务内完成，并推动版本号）
# ---------------------------------------------------------------------------

def import_samples(rows: Iterable[tuple[str, str, str]], mode: str) -> dict[str, Any]:
    """导入样本。

    mode='replace' 全量替换；mode='append' 追加（重复 sample_id 报错）。
    rows 已在接口层完成上限与字段校验。
    """
    rows = list(rows)
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if mode == "replace":
            conn.execute("DELETE FROM subject_freeze")
            conn.execute("DELETE FROM samples")
        existing = {
            r["sample_id"]
            for r in conn.execute("SELECT sample_id FROM samples").fetchall()
        }
        seen: set[str] = set()
        for sample_id, subject_id, disease in rows:
            if sample_id in existing or sample_id in seen:
                raise ServiceError(
                    409, f"sample_id 重复或已存在: {sample_id}"
                )
            seen.add(sample_id)
        conn.executemany(
            "INSERT INTO samples (sample_id, subject_id, disease) VALUES (?, ?, ?)",
            rows,
        )
        _check_caps(conn)
        version = bump_version(conn)
        conn.execute("COMMIT")
    except Exception:
        _rollback_safely(conn)
        raise
    finally:
        conn.close()
    return {"imported": len(rows), "mode": mode, "data_version": version}


def update_sample(sample_id: str, disease: str | None, subject_id: str | None) -> dict[str, Any]:
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT sample_id, subject_id, disease FROM samples WHERE sample_id = ?",
            (sample_id,),
        ).fetchone()
        if row is None:
            raise ServiceError(404, f"sample_id 不存在: {sample_id}")
        new_disease = disease if disease is not None else row["disease"]
        new_subject = subject_id if subject_id is not None else row["subject_id"]
        conn.execute(
            "UPDATE samples SET disease = ?, subject_id = ? WHERE sample_id = ?",
            (new_disease, new_subject, sample_id),
        )
        _check_caps(conn)
        version = bump_version(conn)
        conn.execute("COMMIT")
    except Exception:
        _rollback_safely(conn)
        raise
    finally:
        conn.close()
    return {
        "sample_id": sample_id,
        "subject_id": new_subject,
        "disease": new_disease,
        "data_version": version,
    }


def delete_sample(sample_id: str) -> dict[str, Any]:
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("DELETE FROM samples WHERE sample_id = ?", (sample_id,))
        if cur.rowcount == 0:
            raise ServiceError(404, f"sample_id 不存在: {sample_id}")
        # 清理可能已无样本的冻结受试者
        conn.execute(
            """DELETE FROM subject_freeze
               WHERE subject_id NOT IN (SELECT DISTINCT subject_id FROM samples)"""
        )
        version = bump_version(conn)
        conn.execute("COMMIT")
    except Exception:
        _rollback_safely(conn)
        raise
    finally:
        conn.close()
    return {"deleted": sample_id, "data_version": version}


def set_freeze(subject_id: str, group_name: str) -> dict[str, Any]:
    if group_name not in GROUPS:
        raise ServiceError(422, f"group_name 必须是 {GROUPS} 之一")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute(
            "SELECT 1 FROM samples WHERE subject_id = ? LIMIT 1", (subject_id,)
        ).fetchone()
        if exists is None:
            raise ServiceError(404, f"subject_id 不存在: {subject_id}")
        conn.execute(
            "INSERT INTO subject_freeze (subject_id, group_name) VALUES (?, ?) "
            "ON CONFLICT(subject_id) DO UPDATE SET group_name = excluded.group_name",
            (subject_id, group_name),
        )
        version = bump_version(conn)
        conn.execute("COMMIT")
    except Exception:
        _rollback_safely(conn)
        raise
    finally:
        conn.close()
    return {"subject_id": subject_id, "group": group_name, "data_version": version}


def clear_freeze(subject_id: str) -> dict[str, Any]:
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "DELETE FROM subject_freeze WHERE subject_id = ?", (subject_id,)
        )
        if cur.rowcount == 0:
            raise ServiceError(404, f"subject_id 未被冻结: {subject_id}")
        version = bump_version(conn)
        conn.execute("COMMIT")
    except Exception:
        _rollback_safely(conn)
        raise
    finally:
        conn.close()
    return {"subject_id": subject_id, "frozen": False, "data_version": version}


def _check_caps(conn: sqlite3.Connection) -> None:
    n_samples = conn.execute("SELECT COUNT(*) c FROM samples").fetchone()["c"]
    n_subjects = conn.execute(
        "SELECT COUNT(DISTINCT subject_id) c FROM samples"
    ).fetchone()["c"]
    n_diseases = conn.execute(
        "SELECT COUNT(DISTINCT disease) c FROM samples"
    ).fetchone()["c"]
    if n_samples > MAX_SAMPLES:
        raise ServiceError(422, f"样本数超过上限 {MAX_SAMPLES}")
    if n_subjects > MAX_SUBJECTS:
        raise ServiceError(422, f"受试者数超过上限 {MAX_SUBJECTS}")
    if n_diseases > MAX_DISEASES:
        raise ServiceError(422, f"病种数超过上限 {MAX_DISEASES}")


# ---------------------------------------------------------------------------
# 快照读取
# ---------------------------------------------------------------------------

def take_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """读取求解时刻的完整输入。一次短事务，求解线程随后不再访问数据库。"""
    samples = [
        {"sample_id": r["sample_id"], "subject_id": r["subject_id"], "disease": r["disease"]}
        for r in conn.execute(
            "SELECT sample_id, subject_id, disease FROM samples ORDER BY sample_id"
        ).fetchall()
    ]
    freezes = {
        r["subject_id"]: r["group_name"]
        for r in conn.execute(
            "SELECT subject_id, group_name FROM subject_freeze ORDER BY subject_id"
        ).fetchall()
    }
    version = current_version(conn)
    return {"data_version": version, "samples": samples, "freezes": freezes}


def list_samples() -> list[dict[str, str]]:
    conn = get_conn()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT sample_id, subject_id, disease FROM samples ORDER BY sample_id"
            ).fetchall()
        ]
    finally:
        conn.close()


def list_subjects() -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT s.subject_id,
                   COUNT(*) AS sample_count,
                   GROUP_CONCAT(DISTINCT s.disease) AS diseases,
                   f.group_name AS frozen_group
            FROM samples s
            LEFT JOIN subject_freeze f ON f.subject_id = s.subject_id
            GROUP BY s.subject_id
            ORDER BY s.subject_id
            """
        ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "subject_id": r["subject_id"],
                    "sample_count": r["sample_count"],
                    "diseases": sorted((r["diseases"] or "").split(",")) if r["diseases"] else [],
                    "frozen_group": r["frozen_group"],
                }
            )
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 候选 / 发布版本存取
# ---------------------------------------------------------------------------

def create_solution_row(
    conn: sqlite3.Connection,
    status: str,
    input_version: int,
    quota_request: dict[str, Any],
    snapshot: dict[str, Any],
) -> int:
    cur = conn.execute(
        """INSERT INTO solutions (kind, status, input_version, quota_request, input_snapshot, heartbeat)
           VALUES ('candidate', ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))""",
        (status, input_version, json.dumps(quota_request, ensure_ascii=False),
         json.dumps(snapshot, ensure_ascii=False)),
    )
    return cur.lastrowid


def claim_pending_solution(conn: sqlite3.Connection, worker_id: str) -> int | None:
    """原子认领一个尚无 worker 的 solving 行（跨进程竞争安全）。"""
    cur = conn.execute(
        """UPDATE solutions
              SET worker_id = ?,
                  heartbeat = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = (
                SELECT id FROM solutions
                 WHERE kind = 'candidate' AND status = 'solving' AND worker_id IS NULL
                 ORDER BY id LIMIT 1)
            RETURNING id""",
        (worker_id,),
    )
    row = cur.fetchone()
    return row["id"] if row else None


def touch_heartbeat(conn: sqlite3.Connection, solution_id: int,
                    worker_id: str | None = None) -> int:
    """刷新心跳。只有当前仍归属该 worker 的活跃任务才会被更新，
    被新求解取代（superseded）或已被其他进程接管的行不会被动到。返回受影响行数。"""
    if worker_id is None:
        cur = conn.execute(
            "UPDATE solutions SET heartbeat = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE id = ?",
            (solution_id,),
        )
    else:
        cur = conn.execute(
            "UPDATE solutions SET heartbeat = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE id = ? AND worker_id = ? AND status IN ('solving', 'paused')",
            (solution_id, worker_id),
        )
    return cur.rowcount


def guarded_transition(
    conn: sqlite3.Connection,
    solution_id: int,
    worker_id: str,
    expected_statuses: tuple[str, ...],
    **fields: Any,
) -> bool:
    """仅在任务仍处于期望状态、且仍归属该 worker 时写入状态变更。

    这是防止"旧求解复活"的核心：任务被新求解标记为 superseded（或被其他
    worker 接管）后，旧 worker 迟到的成功/暂停/取消/异常写入全部空转，
    终态由这条 WHERE 条件保证不可被覆盖。返回是否真的完成了写入。
    """
    if not fields:
        raise ValueError("guarded_transition requires at least one field")
    assignments: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        if value is SQLITE_NOW:
            assignments.append(_set_clause(key))
            continue
        assignments.append(f"{key} = ?")
        values.append(
            json.dumps(value, ensure_ascii=False)
            if key in ("assignments", "input_snapshot", "quota_request")
            and value is not None
            else value
        )
    placeholders = ", ".join("?" for _ in expected_statuses)
    values.extend([solution_id, worker_id])
    cur = conn.execute(
        f"UPDATE solutions SET {', '.join(assignments)} "
        f"WHERE id = ? AND worker_id = ? AND status IN ({placeholders})",
        values + list(expected_statuses),
    )
    return cur.rowcount > 0


def write_control(conn: sqlite3.Connection, solution_id: int, signal: str) -> None:
    """写入/更新某个任务自己的控制信号（按任务隔离，互不覆盖）。"""
    conn.execute(
        "INSERT INTO solve_controls (solution_id, signal) VALUES (?, ?) "
        "ON CONFLICT(solution_id) DO UPDATE SET signal = excluded.signal",
        (solution_id, signal),
    )


def read_control(conn: sqlite3.Connection, solution_id: int) -> str | None:
    row = conn.execute(
        "SELECT signal FROM solve_controls WHERE solution_id = ?",
        (solution_id,),
    ).fetchone()
    return row["signal"] if row else None


def clear_control(conn: sqlite3.Connection, solution_id: int) -> None:
    """只删除该任务自己的控制行，绝不触碰其他任务（如新任务）的信号。"""
    conn.execute(
        "DELETE FROM solve_controls WHERE solution_id = ?", (solution_id,)
    )


def get_solution(conn: sqlite3.Connection, solution_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM solutions WHERE id = ?", (solution_id,)
    ).fetchone()


def get_latest(conn: sqlite3.Connection, kind: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM solutions WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)
    ).fetchone()


def update_solution(conn: sqlite3.Connection, solution_id: int, **fields: Any) -> None:
    if not fields:
        return
    assignments = []
    values = []
    for key, value in fields.items():
        assignments.append(f"{key} = ?")
        values.append(
            json.dumps(value, ensure_ascii=False)
            if key in ("assignments", "input_snapshot", "quota_request")
            and value is not None
            else value
        )
    values.append(solution_id)
    conn.execute(
        f"UPDATE solutions SET {', '.join(assignments)} WHERE id = ?", values
    )


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for key in ("quota_request", "input_snapshot", "assignments"):
        if d.get(key):
            d[key] = json.loads(d[key])
    return d
