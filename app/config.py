"""全局配置与业务常量。"""
import os

# 业务硬约束（需求规格）
MAX_SAMPLES = 200
MAX_SUBJECTS = 20
MAX_DISEASES = 4

# 三个集合的固定顺序与合法取值
GROUPS = ("train", "validation", "test")

# SQLite 数据库文件路径，两个 API 进程通过同一个共享文件读写。
# 允许运行时通过 set_db_path() 切换（测试隔离用）；生产环境只在启动时读取环境变量。
_DB_PATH = os.environ.get(
    "SLICE_DB_PATH", os.path.join(os.getcwd(), "slices.db")
)


def get_db_path() -> str:
    return _DB_PATH


def set_db_path(path: str) -> None:
    global _DB_PATH
    _DB_PATH = path
