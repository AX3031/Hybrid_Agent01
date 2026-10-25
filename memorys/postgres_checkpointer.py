"""
PostgreSQL checkpointer：把 LangGraph 的会话状态持久化到 PostgreSQL。

默认不启用；在 .env 中设置 USE_POSTGRES_CHECKPOINTER=true 后由 ReactAgent 延迟加载，
这样未安装 psycopg / 没有数据库时项目仍可正常启动。
"""
import atexit
import os

from dotenv import load_dotenv
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool

load_dotenv()

DB_URL_POSTGRES = os.getenv("DB_URL_POSTGRES")

_pool: ConnectionPool | None = None
_checkpointer: PostgresSaver | None = None


def get_checkpointer() -> PostgresSaver:
    """创建（并缓存）PostgreSQL checkpointer，首次调用时建表。"""
    global _pool, _checkpointer

    if _checkpointer is not None:
        return _checkpointer

    if not DB_URL_POSTGRES:
        raise RuntimeError(
            "未配置 DB_URL_POSTGRES，请在 .env 中添加，例如："
            "DB_URL_POSTGRES=postgresql://postgres:your_password@localhost:5432/postgres?sslmode=disable"
        )

    # PostgresSaver 必须使用 autocommit 连接；
    # prepare_threshold=0 关闭预编译语句缓存，避免连接池复用连接时报 DuplicatePreparedStatement
    # timeout 控制取连接的等待上限：数据库未启动时尽快失败，由 UI 回退到短期记忆
    _pool = ConnectionPool(
        conninfo=DB_URL_POSTGRES,
        min_size=1,
        max_size=10,
        timeout=5,
        kwargs={"autocommit": True, "prepare_threshold": 0},
    )
    # 进程退出前先关闭连接池，否则 Python 3.14 解释器关闭阶段会因 join 线程报错
    atexit.register(_pool.close)

    # 注意：PostgresSaver.from_conn_string() 返回的是上下文管理器，不能直接赋值后长期使用，
    # 所以常驻服务用连接池来持有连接
    _checkpointer = PostgresSaver(_pool)

    # 创建 checkpoint 相关数据表，重复执行是幂等的
    _checkpointer.setup()

    return _checkpointer


def close_pool() -> None:
    """显式关闭连接池（测试或优雅退出时使用）。"""
    global _pool, _checkpointer
    if _pool is not None:
        _pool.close()
        _pool = None
        _checkpointer = None


if __name__ == "__main__":
    get_checkpointer()
    # 只打印 @ 之后的部分，避免把密码输出到日志
    print("PostgreSQL checkpointer 初始化完成：", DB_URL_POSTGRES.rsplit("@", 1)[-1])
