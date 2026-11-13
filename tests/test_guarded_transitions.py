"""持久化层守卫原语的单元测试：guarded_transition / 按任务隔离的控制通道。

这些是"旧求解不可复活"的最后一道防线，直接对数据库函数做断言，
不依赖线程时序。
"""
from __future__ import annotations

import json

import pytest

from app import database as db


@pytest.fixture()
def conn(tmp_path):
    from app import config
    config.set_db_path(str(tmp_path / "guard.db"))
    db.reset_for_test()
    db.init_db()
    c = db.get_conn()
    try:
        yield c
    finally:
        c.close()


def _make_candidate(conn, status="solving", worker="w-1") -> int:
    snap = {"data_version": 1, "samples": [], "freezes": {}}
    cur = conn.execute(
        """INSERT INTO solutions
           (kind, status, input_version, quota_request, input_snapshot, worker_id)
           VALUES ('candidate', ?, 1, ?, ?, ?)""",
        (status, json.dumps({"quota": {}}), json.dumps(snap), worker),
    )
    return cur.lastrowid


def test_guarded_transition_blocks_write_after_supersede(conn):
    sid = _make_candidate(conn)
    # 新求解把旧候选置为 superseded（worker_id 保留旧值，模拟提交事务行为）
    conn.execute("UPDATE solutions SET status='superseded' WHERE id=?", (sid,))

    # 旧 worker 迟到的 solved 写入必须空转
    moved = db.guarded_transition(
        conn, sid, "w-1", ("solving",), status="solved",
        assignments={"s": "train"},
    )
    assert moved is False
    row = conn.execute("SELECT status, assignments FROM solutions WHERE id=?",
                       (sid,)).fetchone()
    assert row["status"] == "superseded"
    assert row["assignments"] is None


def test_guarded_transition_blocks_write_after_worker_takeover(conn):
    sid = _make_candidate(conn, worker="w-old")
    # 孤儿被另一个 worker 接管
    conn.execute("UPDATE solutions SET worker_id='w-new' WHERE id=?", (sid,))

    moved = db.guarded_transition(
        conn, sid, "w-old", ("solving",), status="failed",
        error_message="late crash",
    )
    assert moved is False
    row = conn.execute("SELECT status, worker_id FROM solutions WHERE id=?",
                       (sid,)).fetchone()
    assert row["status"] == "solving"
    assert row["worker_id"] == "w-new"

    # 新 worker 的正常收尾可以写入
    moved = db.guarded_transition(
        conn, sid, "w-new", ("solving",), status="solved",
        assignments={"s": "test"},
    )
    assert moved is True
    assert conn.execute("SELECT status FROM solutions WHERE id=?",
                        (sid,)).fetchone()["status"] == "solved"


def test_guarded_transition_heartbeat_sentinel(conn):
    sid = _make_candidate(conn)
    moved = db.guarded_transition(
        conn, sid, "w-1", ("solving", "paused"),
        status="paused", heartbeat=db.SQLITE_NOW,
    )
    assert moved is True
    row = conn.execute("SELECT status, heartbeat FROM solutions WHERE id=?",
                       (sid,)).fetchone()
    assert row["status"] == "paused"
    assert row["heartbeat"]  # 时间戳表达式已展开


def test_touch_heartbeat_respects_ownership(conn):
    sid = _make_candidate(conn, worker="w-1")
    assert db.touch_heartbeat(conn, sid, "w-1") == 1
    # 接管后旧 worker 的心跳空转
    conn.execute("UPDATE solutions SET worker_id='w-2' WHERE id=?", (sid,))
    assert db.touch_heartbeat(conn, sid, "w-1") == 0
    # 新 worker 在任务活跃时可以刷新
    assert db.touch_heartbeat(conn, sid, "w-2") == 1
    # 进入终态（superseded）后任何心跳都空转
    conn.execute("UPDATE solutions SET status='superseded' WHERE id=?", (sid,))
    assert db.touch_heartbeat(conn, sid, "w-2") == 0


def test_control_rows_are_isolated_per_task(conn):
    old_id, new_id = _make_candidate(conn), _make_candidate(conn, worker="w-2")
    db.write_control(conn, new_id, "pause")
    db.write_control(conn, old_id, "cancel")

    assert db.read_control(conn, old_id) == "cancel"
    assert db.read_control(conn, new_id) == "pause"
    assert db.read_control(conn, 9999) is None

    # 旧任务收尾只清自己的行，新任务的 pause 必须保留
    db.clear_control(conn, old_id)
    assert db.read_control(conn, old_id) is None
    assert db.read_control(conn, new_id) == "pause"

    # 同任务重复写信号为覆盖语义（upsert）
    db.write_control(conn, new_id, "resume")
    assert db.read_control(conn, new_id) == "resume"
    n = conn.execute("SELECT COUNT(*) c FROM solve_controls").fetchone()["c"]
    assert n == 1


def test_guarded_transition_requires_fields(conn):
    sid = _make_candidate(conn)
    with pytest.raises(ValueError):
        db.guarded_transition(conn, sid, "w-1", ("solving",))
