"""会话记忆方式的统一工厂。

三种方式：
- short_term：LangGraph InMemorySaver，进程内有效，重启即丢失（默认）
- file：JSON 文件持久化，写入 memorys/chat_history/agent_checkpoints.json
- postgres：PostgreSQL checkpointer，需要在 .env 配置 DB_URL_POSTGRES
"""
from langgraph.checkpoint.memory import InMemorySaver

DEFAULT_MEMORY_MODE = "short_term"

# 顺序即 Streamlit 下拉/单选的展示顺序，第一个为默认值
MEMORY_MODE_LABELS = {
    "短期记忆（默认，不存档记忆）": "short_term",
    "本地记忆（以JSON格式本地存档）": "file",
    "PostgreSQL（存档入数据库，需配置）": "postgres",
}


def create_checkpointer(memory_mode: str = DEFAULT_MEMORY_MODE):
    """按记忆方式创建 checkpointer；可选依赖在对应分支中延迟导入。"""
    if memory_mode == "short_term":
        return InMemorySaver()

    if memory_mode == "file":
        from memorys.file_checkpointer import get_file_checkpointer

        return get_file_checkpointer()

    if memory_mode == "postgres":
        # 未安装 psycopg 或未配置数据库时，不影响其他记忆方式启动
        from memorys.postgres_checkpointer import get_checkpointer

        return get_checkpointer()

    raise ValueError(
        f"未知的会话记忆方式：{memory_mode}，可选值：short_term / file / postgres"
    )
