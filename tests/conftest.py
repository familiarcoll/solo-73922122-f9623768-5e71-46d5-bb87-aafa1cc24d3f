"""pytest 全局夹具：每个测试函数使用独立的临时数据库。

应用模块保持单例，通过 config.set_db_path() 切换底层文件，
并重置 database 的初始化标记，避免 importlib.reload 带来的类身份不一致问题。
"""
import os
import tempfile
import uuid

import pytest


@pytest.fixture()
def client():
    tmp_dir = tempfile.mkdtemp(prefix="slicetest-")
    db_path = os.path.join(tmp_dir, f"test-{uuid.uuid4().hex[:8]}.db")

    from fastapi.testclient import TestClient

    from app import config, database
    from app.main import app
    from app.manager import manager

    config.set_db_path(db_path)
    database.reset_for_test()
    database.init_db()
    manager.reset()
    manager.start()
    with TestClient(app) as c:
        yield c
    manager.stop()
