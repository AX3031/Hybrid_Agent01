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
- `agent/tools/os_tools.py`：13 个电脑操作工具 —— 只读（`get_system_info`、`list_directory`、`read_text_file`、`search_files`、`search_text`、`list_processes`）与有副作用（`write_text_file`、`create_directory`、`move_path`、`copy_path`、`delete_path`、`run_command`、`open_path`）。
- `agent/react_agent.py`：`CONFIRM_TOOLS` + `HumanInTheLoopMiddleware`，高危工具调用前挂起等待确认；新增 `AgentEvent` 与 `resume_stream(decisions)`。
- `app.py`：`_render_events` / `_render_hitl_block`，渲染确认卡片并支持批准/拒绝。
- `config/model.yml`：由 `config/rag.yml` 重命名而来，只保留对话与备用对话模型。
- `docs/OS_TOOLS.md`：电脑操作功能说明，包含普通使用者场景、确认卡片、安全边界、FAQ、工具参数表与扩展方法。

### Changed

- 三种会话记忆统一走 LangGraph checkpointer（`short_term` → `InMemorySaver`；`file` → `FileCheckpointSaver`；`postgres` → 原有 `PostgresSaver`）；`execute_stream` 每次只传当前一条 `HumanMessage`，历史由 `thread_id` 自动恢复。
- `memorys/postgres_checkpointer.py`：连接池 `timeout` 由 30 秒改为 5 秒，数据库未启动时快速失败并由界面回退为短期记忆，不再卡住页面。
- `memorys/temporary_memory.py`、`memorys/persistent_memory.py` 保留为示例、不接入主链路：其接口是 `BaseChatMessageHistory`，缺少 `get_tuple/put/put_writes/list/delete_thread`，且 `InMemoryChatMessageHistory` 在 LangChain 1.6.4 已标记废弃。
- `config/rag.yml`：`backup_chat_model_name` 由 `deepseek-v4-flash-0731` 改为 `glm-5`。
- 记忆方式的界面文案（`memorys/memory_factory.py`、`app.py`）：选项改为「短期记忆（默认，不存档记忆）/ 本地记忆（以JSON格式本地存档）/ PostgreSQL（存档入数据库，需配置）」；短期记忆的提示补上“刷新页面”也会丢（Streamlit 刷新会重建 `st.session_state` 与 Agent）；弹层 caption 不再重复显示会话 ID。
- 文档同步：`README.md`（能力表、4.5 启动、配置速查、目录树与文档索引）、`docs/PROJECT_OVERVIEW.md`（分层、关键约束、问题索引）、`docs/ARCHITECTURE.md`（5.2、5.3、6.4、9.2、9.6、12、13.4、16.1、16.3）、`docs/architecture.svg`（记忆方式说明、文件型会话历史状态）。
- `.gitignore`：忽略运行时生成的 `memorys/chat_history/`。
- 项目定位由“机器人智能客服”调整为“本地电脑助手”；`prompts/main_prompt.txt` 重写为电脑操作规则与安全边界；`app.py` 的标题、欢迎语与输入框文案同步更新。
- `ReactAgent` 改用 `stream_mode=["values", "updates"]`，同时捕获文本与 `__interrupt__`。
- `model/factory.py` 与 `agent/tools/middleware.py` 移除 `EmbeddingsFactory` / `embed_model`，`rag_conf` 改名 `model_conf`。
- 依赖：移除 `dashscope`、`langchain-chroma`、`langchain-community`、`pypdf`；新增 `psutil`。
- 文档同步：`README.md`（定位、能力表、技术栈、快速开始、目录树、阅读路线、配置速查、限制、文档索引）、`docs/PROJECT_OVERVIEW.md`（技术栈、分层、主流程、电脑操作、目录、约束、问题索引）、`docs/ARCHITECTURE.md`（2.3、3、4、5、6、7、8.3、9、10、11、12、13、14、15、16）、`docs/architecture.svg`（标题、工具层、服务层、模型/存储层、副标题与版本快照）、`AGENTS.md`（阅读顺序与文档动作）。

### Removed

- `agent/react_agent.py` 的 `_build_checkpointer()`：checkpointer 的创建改由 `memorys/memory_factory.py` 负责。
- RAG 子系统：`rag/rag_service.py`、`rag/vector_store.py`、`rag_summarize` 工具、`prompts/rag_summarize.txt`、`config/chroma.yml`、`utils/file_handler.py`、`utils/prompt_loader.load_rag_prompts`。
- `config/prompts.yml` 的 `rag_summarize_prompt_path` 配置项；`memorys/temporary_memory.py`、`persistent_memory.py` 中残留的 `load_rag_prompts` 引用。
- 数据与产物：`chroma_db/`、`md5.text`、`data/测试文本.txt`、`data/测试pdf2.pdf`、`docs/rag-pipeline.svg`。
- `prompts/main_prompt.txt` / `report_prompt.txt` 中的知识库/RAG 文案。

### Fixed

- 聊天界面不再回显用户问题与工具中间结果，只输出不带 `tool_calls` 的最终 `AIMessage` 文本（技术债 TD-03）。
- 修复回答为空时 `response_messages[-1]` 抛出的 `IndexError`（技术债 TD-04）。
