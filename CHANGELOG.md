# Changelog

本文件记录 project2 的所有重要变更。

格式遵循 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)（分组标题保留英文，便于工具解析），版本号遵循 [Semantic Versioning](https://semver.org/spec/v2.0.0.html)。仓库当前没有 tag，未发布的改动累积在 `## [Unreleased]`；发布时把该节整体挪到新的版本标题下（例如 `## [0.1.0] - 2026-10-23`），并在文末补 compare 链接。

## [Unreleased]

### Added

- `memorys/memory_factory.py`：`create_checkpointer(memory_mode)` 统一入口，`DEFAULT_MEMORY_MODE = "short_term"`、`MEMORY_MODE_LABELS`（界面文案 ↔ 模式值）。
- `memorys/file_checkpointer.py`：`FileCheckpointSaver` —— 继承 LangGraph `InMemorySaver`，把 `storage/writes/blobs` 序列化为 JSON 并原子写入 `memorys/chat_history/agent_checkpoints.json`（`bytes ↔ base64`、`tuple ↔ 标签结构`、非字符串 dict 键 ↔ 键值对列表；临时文件 + `os.replace`；`threading.RLock`；进程内单例 `get_file_checkpointer()`）。
- `agent/react_agent.py`：`ReactAgent(memory_mode=...)`、`clear_memory(thread_id)`、`load_ui_messages(thread_id)`；`_default_memory_mode()` 兼容读取 `USE_POSTGRES_CHECKPOINTER`。
- `app.py`：对话输入框左侧的记忆按钮（`st.bottom` + 水平容器 + `st.popover` + `st.radio`），点击弹出三种记忆方式列表；侧边栏「会话管理」= 会话 ID、新建会话、清空记忆。
- `CHANGELOG.md`。

### Changed

- 三种会话记忆统一走 LangGraph checkpointer（`short_term` → `InMemorySaver`；`file` → `FileCheckpointSaver`；`postgres` → 原有 `PostgresSaver`）；`execute_stream` 每次只传当前一条 `HumanMessage`，历史由 `thread_id` 自动恢复。
- `memorys/postgres_checkpointer.py`：连接池 `timeout` 由 30 秒改为 5 秒，数据库未启动时快速失败并由界面回退为短期记忆，不再卡住页面。
- `memorys/temporary_memory.py`、`memorys/persistent_memory.py` 保留为示例、不接入主链路：其接口是 `BaseChatMessageHistory`，缺少 `get_tuple/put/put_writes/list/delete_thread`，且 `InMemoryChatMessageHistory` 在 LangChain 1.6.4 已标记废弃。
- `config/rag.yml`：`backup_chat_model_name` 由 `deepseek-v4-flash-0731` 改为 `glm-5`。
- 记忆方式的界面文案（`memorys/memory_factory.py`、`app.py`）：选项改为「短期记忆（默认，不存档记忆）/ 本地记忆（以JSON格式本地存档）/ PostgreSQL（存档入数据库，需配置）」；短期记忆的提示补上“刷新页面”也会丢（Streamlit 刷新会重建 `st.session_state` 与 Agent）；弹层 caption 不再重复显示会话 ID。
- 文档同步：`README.md`（能力表、4.5 启动、配置速查、目录树与文档索引）、`docs/PROJECT_OVERVIEW.md`（分层、关键约束、问题索引）、`docs/ARCHITECTURE.md`（5.2、5.3、6.4、9.2、9.6、12、13.4、16.1、16.3）、`docs/architecture.svg`（记忆方式说明、文件型会话历史状态）。
- `.gitignore`：忽略运行时生成的 `memorys/chat_history/`。

### Removed

- `agent/react_agent.py` 的 `_build_checkpointer()`：checkpointer 的创建改由 `memorys/memory_factory.py` 负责。

### Fixed

- 聊天界面不再回显用户问题与工具中间结果，只输出不带 `tool_calls` 的最终 `AIMessage` 文本（技术债 TD-03）。
- 修复回答为空时 `response_messages[-1]` 抛出的 `IndexError`（技术债 TD-04）。
