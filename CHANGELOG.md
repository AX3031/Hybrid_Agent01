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
- `docs/SECURITY_REPORT.md`：安全评估报告，包含会做/能做/不会做/不能做矩阵、当前措施实现、STRIDE 与 OWASP LLM Top 10 映射、技术债与 P0–P2 改进路线。
- `agent/tools/safety.py`：工作区域、敏感目录、系统目录、可执行后缀、危险命令黑名单、越界路径检查、覆盖备份、回收区，以及 `describe_operation` / `operation_risk` / `operation_target` 人话解释。
- `utils/audit_logger.py`：每次工具调用写入 `logs/audit_YYYYMMDD.jsonl`（时间、会话、工具、参数、状态、结果摘要）。
- `config/agent.yml`：新增 `workspace_roots` 与 `sensitive_dirs`。
- `app.py`：新增「本次操作记录」面板与大白话确认卡片（目标、风险、影响范围）；侧边栏展示工作区域；按钮改为「我确认，执行 / 不要执行」。
- 删除回收区：`delete_path` 默认移到 `.agent_trash`，`permanent=True` 才永久删除。
- 覆盖前自动备份到 `.agent_backup`（10MB 上限）。
- `ToolCallLimitMiddleware`：单轮最多 20 次工具调用。
- `app.py`：输入框左侧新增「工作区域」弹层，可查看当前范围、每行输入一个绝对路径、保存后立即生效并写回 `config/agent.yml`。
- `agent/tools/safety.py`：新增 `validate_workspace_inputs` / `save_workspace_roots` / `reload_workspace_roots`；输入校验覆盖绝对路径、盘符根、系统保护目录。
- `get_system_info`、`list_processes` 加入确认清单，并补充“会读取设备标识/正在使用的软件”的风险说明。

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
- `agent/tools/os_tools.py`：所有路径工具接入 `guard_path`；`delete_path` 增加 `permanent`；`write_text_file` 增加后缀检查与覆盖备份；`run_command` 增加命令黑名单与越界路径检查。
- `agent/react_agent.py`：新增 `ToolCallLimitMiddleware`、`AgentEvent(type="tool")`、`resume_stream(pending_actions=...)`；中间件从 7 个增为 8 个。
- `agent/tools/middleware.py`：`monitor_tool` 修正日志时序，并接入审计日志。
- `prompts/main_prompt.txt`：补充工作区域、回收区、备份、可执行后缀、命令拦截、20 次上限、操作留痕等安全规则。
- 文档同步：`docs/OS_TOOLS.md` 重写为「功能与安全说明」；`README.md` 能力表/配置速查/限制；`ARCHITECTURE.md` 7/9.3/10.4/11.5/12/16.1；`PROJECT_OVERVIEW.md` 5/6/7；`architecture.svg` 安全节点；`AGENTS.md` 安全策略同步规则。
- `CONFIRM_TOOLS` 从 7 个增加到 9 个；`get_system_info`、`list_processes` 从“直接执行”改为“确认后执行”。
- `docs/OS_TOOLS.md` 第 2 节扩充为 2.1–2.8：为什么需要工作区域、默认范围、查看方式、两种修改方式、允许/拒绝例子、填写规则、常见错误、敏感目录。
- 文档与图同步：README/ARCHITECTURE 目录树补充 `safety.py`、`audit_logger.py`；ARCHITECTURE 5.3 依赖图加入 `safety.py`、`audit_logger.py`；`architecture.svg` 表现层标注工作区域设置、编排层补充 ToolCallLimit、底部黑名单数字更新。

### Removed

- `agent/react_agent.py` 的 `_build_checkpointer()`：checkpointer 的创建改由 `memorys/memory_factory.py` 负责。
- RAG 子系统：`rag/rag_service.py`、`rag/vector_store.py`、`rag_summarize` 工具、`prompts/rag_summarize.txt`、`config/chroma.yml`、`utils/file_handler.py`、`utils/prompt_loader.load_rag_prompts`。
- `config/prompts.yml` 的 `rag_summarize_prompt_path` 配置项；`memorys/temporary_memory.py`、`persistent_memory.py` 中残留的 `load_rag_prompts` 引用。
- 数据与产物：`chroma_db/`、`md5.text`、`data/测试文本.txt`、`data/测试pdf2.pdf`、`docs/rag-pipeline.svg`。
- `prompts/main_prompt.txt` / `report_prompt.txt` 中的知识库/RAG 文案。

### Fixed

- 聊天界面不再回显用户问题与工具中间结果，只输出不带 `tool_calls` 的最终 `AIMessage` 文本（技术债 TD-03）。
- 修复回答为空时 `response_messages[-1]` 抛出的 `IndexError`（技术债 TD-04）。
- `monitor_tool` 不再在工具执行前打印“调用成功”，改为“开始执行”与“调用成功”两条日志（技术债 TD-12）。
- 命令越界检查同时支持反斜杠和正斜杠路径，修复 `C:/Windows/...` 可绕过的问题。
- `run_command` 未指定 `cwd` 时默认在第一个工作区根目录执行，不再继承应用启动目录。
- 恢复执行时按工具名匹配待确认操作，避免只读工具先返回时错配操作记录。
- `expand_path` 将相对路径统一基于项目根解析，避免启动目录不同导致路径漂移。
- 最新一轮的操作记录默认展开，历史记录折叠；`status` 为空时按成功处理。
- 危险命令黑名单从 15 条增加到 28 条，补齐环境变量（`%SystemRoot%`）、PowerShell 编码命令（`-EncodedCommand`）、`cd ..`、`copy con`、重定向到可执行后缀、`Set-Content`/`Out-File`、下载/执行工具（`certutil`/`mshta`/`rundll32` 等）、cmd 转义符 `^`、`attrib`/`icacls`/`wmic`/`Start-Process` 等绕过路径。
- 禁止删除、移动、复制工作区域根目录本身，避免误操作整个工作区。
- 工作区域不允许设置为敏感目录（`.ssh`、`.aws` 等）。
