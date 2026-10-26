# 本地电脑助手 Agent

基于 **LangChain 1.x `create_agent`（LangGraph）** 构建的工具调用型本地电脑助手：支持系统信息查询、文件读写与搜索、进程查看、命令执行（需确认），以及 Streamlit 对话界面。

> 这是一份面向开发者的项目入口文档。5 分钟速览见 [docs/PROJECT_OVERVIEW.md](docs/PROJECT_OVERVIEW.md)；深入原理、配置项、扩展步骤与技术债见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 导读

**先选一条路线**

| 目标 | 建议阅读 | 时间 |
| --- | --- | --- |
| 5 分钟知道项目是什么 | 本文第 1–2 节 + [1 页速览](docs/PROJECT_OVERVIEW.md) | 5 分钟 |
| 把项目跑起来 | 本文第 4 节 + 第 7 节 | 15 分钟 |
| 改代码/加功能 | [ARCHITECTURE](docs/ARCHITECTURE.md) 第 5、6、13 节 | 30–60 分钟 |
| 排障/上线 | [ARCHITECTURE](docs/ARCHITECTURE.md) 第 10、11、12 节 | 按需 |

**文档地图**

| 文件 | 用途 |
| --- | --- |
| [README.md](README.md) | 当前文件：门面、上手指南、命令与配置速查 |
| [docs/PROJECT_OVERVIEW.md](docs/PROJECT_OVERVIEW.md) | 1 页速览，人和 AI 的快速入口 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 当前状态的深度参考：模块、流程、配置、扩展、技术债 |
| [docs/architecture.svg](docs/architecture.svg) | 分层架构图（人看） |
| [docs/OS_TOOLS.md](docs/OS_TOOLS.md) | 电脑操作功能与安全说明（普通人向 + 开发者参数） |
| [AGENTS.md](AGENTS.md) | AI 阅读约定（可选，供编码助手使用） |
| [CHANGELOG.md](CHANGELOG.md) | 重要变更日志（Keep a Changelog 格式，未发布改动在 `[Unreleased]`） |

**本文目录**

1. [项目定位](#1-项目定位)
2. [能力清单](#2-能力清单)
3. [技术栈](#3-技术栈)
4. [快速开始](#4-快速开始)
5. [目录结构](#5-目录结构)
6. [代码阅读路线](#6-代码阅读路线)
7. [配置入口速查](#7-配置入口速查)
8. [已知限制](#8-已知限制)
9. [文档索引](#9-文档索引)
10. [安全与协作约定](#10-安全与协作约定)

---

## 1. 项目定位

| 维度 | 说明 |
| --- | --- |
| 业务形态 | 本地电脑助手：查看系统与进程、浏览/搜索/读写文件、执行命令，并保留原有业务查询与报告能力 |
| 技术形态 | 单进程 Streamlit 应用 + LangGraph Agent + 本地文件系统/psutil |
| 适用范围 | 原型验证、LangChain Agent 学习、个人效率工具 |
| 非目标 | 生产级多租户、鉴权、计费、高可用；当前均未实现 |

**一句话架构**：Streamlit 收集指令 → `ReactAgent` 调用 `create_agent` 构建 LangGraph → 模型在中间件约束下循环调用工具 → 只读工具直接执行、高危工具等待用户确认 → 模型生成答案 → 按消息块流式返回页面。

![系统架构总览](docs/architecture.svg)

---

## 2. 能力清单

| 能力 | 状态 | 实现位置 |
| --- | --- | --- |
| 多轮工具调用（ReAct 循环） | ✅ 已接入 | `agent/react_agent.py` → `create_agent` |
| 系统信息 / 磁盘 / 进程查看 | ✅ 已接入 | `agent/tools/os_tools.py` → `get_system_info`、`list_processes` |
| 目录浏览 / 文本读取 / 文件搜索 | ✅ 已接入 | `list_directory`、`read_text_file`、`search_files`、`search_text` |
| 文件写入 / 移动 / 复制 / 删除 | ✅ 已接入（需确认） | `write_text_file`、`move_path`、`copy_path`、`delete_path` |
| 系统命令执行 | ✅ 已接入（需确认） | `run_command` |
| 高危操作人工确认 | ✅ 已接入 | `HumanInTheLoopMiddleware` + `app.py` 确认卡片 |
| 工作区域限制（读写都受限） | ✅ 已接入 | `agent/tools/safety.py` → `guard_path`；配置 `config/agent.yml` |
| 敏感目录拦截 | ✅ 已接入 | `.ssh` / `.gnupg` / `.aws` 等默认拒绝 |
| 聊天页操作记录 | ✅ 已接入 | `ReactAgent` 的 `tool` 事件 + `app.py` 的 `_render_operations` |
| 结构化审计日志 | ✅ 已接入 | `utils/audit_logger.py` → `logs/audit_YYYYMMDD.jsonl` |
| 删除回收区 / 覆盖备份 | ✅ 已接入 | `.agent_trash` / `.agent_backup` |
| 危险命令与可执行文件拦截 | ✅ 已接入 | `safety.py` 的 `check_command` / `check_write_suffix` |
| 单轮工具调用上限 | ✅ 已接入 | `ToolCallLimitMiddleware`（20 次/轮） |
| 工具调用日志 | ✅ 已接入 | `agent/tools/middleware.py` → `monitor_tool` |
| 模型调用前状态日志 | ✅ 已接入 | `agent/tools/middleware.py` → `log_before_model` |
| 报告场景动态提示词 | ✅ 已接入 | `fill_context_for_report` + `report_prompt_switch` |
| 联网搜索（百度） | ✅ 已接入 | `agent/tools/agent_tools.py` → `web_search_baidu` |
| PII 脱敏（邮箱/手机号） | ✅ 已接入 | `agent/tools/middleware.py` → `PIIMiddleware` |
| 模型降级（主模型 → 备用模型） | ✅ 已接入 | `agent/tools/middleware.py` → `ModelFallbackMiddleware` |
| 外部使用记录查询 | 🧪 示例 | `fetch_external_data` 读取 CSV，示例文件当前为空 |
| 天气 / 城市 / 用户 ID / 月份 | 🧪 模拟 | `agent/tools/agent_tools.py` 中返回随机或固定值 |
| 会话记忆（短期 / 本地 JSON / PostgreSQL） | ✅ 已接入 | `app.py` 输入框左侧按钮切换；`memorys/memory_factory.py`、`file_checkpointer.py`、`postgres_checkpointer.py` |
| 文件上传 UI | 🧪 预留 | `app_file_uploader.py` 当前为空壳 |
| GUI 自动化（窗口/键鼠/截图） | ❌ 未实现 | — |
| 鉴权 / 多用户 / 限流 | ❌ 未实现 | — |

---

## 3. 技术栈

| 层 | 组件 | 版本 | 作用 |
| --- | --- | --- | --- |
| 运行时 | CPython | 3.14（`pyproject` 要求 ≥3.13） | 由 uv 管理的项目虚拟环境 |
| 包管理 | uv | 0.12.9 | 依赖安装、锁文件、命令执行 |
| Agent 框架 | langchain | 1.4.0 | `create_agent`、中间件、工具装饰器 |
| 图执行 | langgraph | 1.2.11 | Agent 的 StateGraph 运行时、`stream` |
| 状态持久化 | langgraph-checkpoint-postgres / psycopg | 3.2.0 / 3.3.6 | PostgreSQL checkpointer 与连接池 |
| 模型接入 | langchain / langchain-openai | 1.4.0 / 1.6.6 | `init_chat_model` 构造官方 `ChatOpenAI`，调用 DashScope OpenAI 兼容端点 |
| 系统信息/进程 | psutil | 7.2.2 | CPU、内存、磁盘、进程枚举 |
| 命令执行 | subprocess（标准库） | — | 执行系统命令并捕获输出 |
| UI | streamlit | 1.64.0 | 对话界面与流式渲染 |
| 配置 | PyYAML / python-dotenv | 6.0.3 / 1.2.3 | YAML 配置、`.env` 加载 |
| HTTP 客户端 | requests | 2.34.2 | 百度联网搜索 |

模型：`qwen3.5-flash`（对话，经 DashScope OpenAI 兼容模式）；`glm-5`（备用对话模型，主模型失败时自动降级）。

---

## 4. 快速开始

### 4.1 前置条件

- Windows / macOS / Linux
- 已安装 [uv](https://docs.astral.sh/uv/)
- 一个可用的 DashScope API Key

### 4.2 安装依赖

在项目根目录执行：

```powershell
uv sync
```

项目已在 `pyproject.toml` 中配置清华 PyPI 镜像；虚拟环境位于 `.venv/`。

> 如果 `uv sync` 重建了 `.venv`，需要把 `env_patch/project2_root.pth` 与 `env_patch/sitecustomize.py` 复制到 `.venv/Lib/site-packages/`（Windows）。否则在 IDE 中直接运行 `.py` 可能报 `ModuleNotFoundError`，Windows 终端也可能出现中文/emoji 编码错误。使用 `uv run -m ...` 从项目根运行可规避导入问题。

### 4.3 准备 `.env`

在项目根目录创建 `.env`（该文件已被 `.gitignore` 忽略，不要提交）：

| 变量名 | 使用方 | 是否必需 |
| --- | --- | --- |
| `OPEN_API_KEY` | `model/factory.py` 中 `init_chat_model(api_key=...)` | 是（注意变量名不是 `OPENAI_API_KEY`） |
| `BASE_URL_DASHSCOPE` | `init_chat_model(base_url=...)` 的 OpenAI 兼容端点 | 是 |
| `DB_URL_POSTGRES` | `memorys/postgres_checkpointer.py` | 否；在 Streamlit 选择 PostgreSQL 记忆方式时必需 |
| `USE_POSTGRES_CHECKPOINTER` | `agent/react_agent.py` | 否；兼容配置，未显式传入 `memory_mode` 时默认使用 PostgreSQL |
| `API_KEY_BAIDU` | `web_search_baidu` | 否；不配置时工具返回空字符串 |
| `BASE_URL_DEEPSEEK`、`API_KEY_DEEPSEEK`、`API_HOST_DASHSCOPE` | 当前代码未使用 | 否，预留 |

### 4.4 启动对话界面

```powershell
uv run streamlit run app.py
```

浏览器打开 Streamlit 输出的地址即可对话。**对话输入框左侧**有一个记忆按钮（显示当前方式），点击后弹出列表可切换：

- **短期记忆（默认，不存档记忆）**：仅在当前进程内按会话 ID 保留上下文，刷新页面或重启即丢失。
- **本地记忆（以JSON格式本地存档）**：写入 `memorys/chat_history/agent_checkpoints.json`，重启后可恢复。
- **PostgreSQL（存档入数据库，需配置）**：使用 `.env` 的 `DB_URL_POSTGRES`，支持跨进程/多实例恢复。

弹层底部显示当前方式的存储位置；左侧栏「会话管理」提供会话 ID、新建会话与清空记忆（后续可在此列出不同会话记录）。相同会话 ID 即可恢复对应历史。

### 4.5 常用自检命令

```powershell
uv run -m agent.react_agent      # 直接跑一次 Agent（会真实调用模型）
uv run -m agent.tools.os_tools   # 验证电脑工具（不调用模型）
uv run -m utils.logger_handler   # 验证日志输出
```

> `agent.react_agent` 会触发真实模型调用，运行前确认 `.env` 已配置；其余两个不调用模型。

---

## 5. 目录结构

```text
project2/
├── README.md                   # ✅ 项目入口、导读与命令
├── AGENTS.md                   # ⚙️ AI 阅读约定
├── CHANGELOG.md                # ✅ 重要变更日志（Keep a Changelog）
├── app.py                      # ✅ Streamlit 对话入口
├── app_file_uploader.py        # 🧪 文件上传入口（空壳，待实现）
├── main.py                     # 🧪 PyCharm 默认模板，无业务逻辑
├── test1.py                    # 🧪 临时脚本
├── pyproject.toml / uv.lock    # 依赖与锁文件
├── .env                        # 本地密钥（git 忽略）
├── agent/
│   ├── react_agent.py          # ✅ Agent 组装、HITL 确认、事件流
│   └── tools/
│       ├── os_tools.py         # ✅ 13 个电脑信息与文件操作工具
│       ├── agent_tools.py      # ✅ 7 个业务工具
│       └── middleware.py       # ✅ 6 个中间件
├── model/
│   └── factory.py              # ✅ Chat 模型工厂
├── memorys/
│   ├── memory_factory.py       # ✅ 记忆方式工厂（短期 / 文件 / PostgreSQL）
│   ├── file_checkpointer.py    # ✅ JSON 文件型 checkpointer
│   ├── postgres_checkpointer.py  # 🧪 PostgreSQL checkpointer（可选）
│   ├── persistent_memory.py    # 🧪 文件型会话历史实现（示例）
│   └── temporary_memory.py     # 🧪 内存型会话历史实现（示例）
├── utils/
│   ├── config_handler.py       # ⚙️ YAML 配置加载
│   ├── prompt_loader.py        # ⚙️ 提示词加载
│   ├── path_tool.py            # ⚙️ 项目根目录/绝对路径
│   └── logger_handler.py       # ⚙️ 控制台 + 按日文件日志
├── config/                     # ⚙️ model / prompts / agent 配置
├── prompts/                    # ⚙️ 系统提示词、报告提示词
├── data/                       # external/ 下为业务 CSV
├── logs/                       # 按日日志
├── env_patch/                  # ⚙️ 虚拟环境补丁（.pth + sitecustomize.py）
└── docs/                       # 速览、架构文档、功能说明与 SVG 图
```

图例：✅ 主链路 / 🧪 示例或预留 / ⚙️ 基础设施。

---

## 6. 代码阅读路线

约 30 分钟，按依赖顺序阅读：

1. `app.py`：看 UI 如何创建 Agent、如何接收流式输出。
2. `agent/react_agent.py`：看 `create_agent` 的模型、工具、中间件三要素。
3. `agent/tools/os_tools.py`：看电脑信息、文件读写、搜索与命令执行如何用 `@tool` 描述，以及路径/系统目录保护。
4. `agent/tools/agent_tools.py`：看原有业务工具与 CSV 懒加载。
5. `agent/tools/middleware.py`：看工具监控、模型前日志、动态提示词切换、PII 脱敏与模型降级。
6. `agent/react_agent.py` 的 `CONFIRM_TOOLS` / `hitl_middleware`：看哪些工具需要人工确认。
7. `model/factory.py` → `config/*.yml`：看模型与参数如何装配。
8. `utils/*`：看配置、路径、日志、提示词加载的公共约定。

更完整的调用时序、配置表、扩展步骤和已知问题见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 7. 配置入口速查

| 想改什么 | 改哪里 |
| --- | --- |
| 换对话模型 | `config/model.yml` + `model/factory.py` |
| 设置工作区域 | `config/agent.yml` 的 `workspace_roots`（改完重启） |
| 设置敏感目录 | `config/agent.yml` 的 `sensitive_dirs` |
| 调整备份/回收区/命令黑名单 | `agent/tools/safety.py` |
| 设置备用对话模型 | `config/model.yml` 的 `backup_chat_model_name` |
| 调整命令超时/读取上限 | `agent/tools/os_tools.py` 顶部常量 |
| 换外部 CSV 路径 | `config/agent.yml` |
| 换提示词 | `prompts/*.txt`（Agent 系统提示词每次模型调用前重读） |
| 新增工具 | `agent/tools/os_tools.py` + `agent/react_agent.py` 的 tools 列表；有副作用时同步加入 `CONFIRM_TOOLS` |
| 新增场景 | 参考报告场景：工具 + 中间件标记 + 动态提示词 + prompt 文件 |
| 切换会话记忆方式 | Streamlit **输入框左侧的记忆按钮**（点击弹出列表）：短期记忆（默认，不存档记忆）/ 本地记忆（以JSON格式本地存档）/ PostgreSQL（存档入数据库，需配置） |
| 管理会话 ID / 新建会话 / 清空记忆 | Streamlit 左侧栏「会话管理」（后续可在此列出会话记录） |
| PostgreSQL 连接 | `.env` 的 `DB_URL_POSTGRES`；`USE_POSTGRES_CHECKPOINTER=true` 仅作为无 UI 场景的默认记忆方式 |
| 百度联网搜索 | `.env` 的 `API_KEY_BAIDU` |

---

## 8. 已知限制

先看这 5 条：

1. **默认是短期记忆**：进程内按会话 ID 保留上下文，刷新页面或重启即丢失；本地记忆/PostgreSQL 模式可跨进程恢复（PostgreSQL 需配置连接串）。
2. **流式输出是“消息块级”而非 token 级**：只输出 `AIMessage` 文本，用户问题与工具中间结果不回显；UI 按事件块渲染。
3. **PII 脱敏覆盖不完整**：`PIIMiddleware` 只处理模型输入/输出，日志与缓存中的敏感信息尚未脱敏。
4. **外部数据示例为空**：`data/external/records.csv` 为空，`fetch_external_data` 会查不到记录；CSV 解析也未处理带逗号的引号字段。
5. **命令检查不是沙箱**：`run_command` 使用黑名单和工作区路径检查，精心构造的命令仍可能绕过；确认卡片是最后一道防线。
6. **工作区限制基于路径比较**：不防符号链接/junction 逃逸，`resolve()` 已尽量解析。

完整清单与修复建议见 [docs/ARCHITECTURE.md#12-已知问题与技术债](docs/ARCHITECTURE.md#12-已知问题与技术债)。

---

## 9. 文档索引

| 文档 | 内容 |
| --- | --- |
| [docs/PROJECT_OVERVIEW.md](docs/PROJECT_OVERVIEW.md) | 1 页速览：定位、分层、主流程、关键约束、问题索引 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 架构、模块职责、调用时序、配置、扩展指南、技术债 |
| [docs/architecture.svg](docs/architecture.svg) | 分层架构总览图 |
| [docs/OS_TOOLS.md](docs/OS_TOOLS.md) | 电脑操作功能与安全说明：工作区域、确认卡片、回收区、审计日志、参数表 |
| [AGENTS.md](AGENTS.md) | AI 阅读约定（可选，供编码助手使用） |
| [CHANGELOG.md](CHANGELOG.md) | 重要变更：加了什么 / 改了什么 / 修了什么 |

---

## 10. 安全与协作约定

- `.env` 已加入 `.gitignore`；不要把 API Key、数据库连接串写进代码、提示词、日志或文档。
- `logs/` 会记录用户输入与工具参数，接入真实业务后需考虑脱敏与保留周期。
- 新增入口脚本建议从项目根目录用 `uv run -m <module>` 运行；直接运行 `.py` 依赖 `env_patch` 注入的项目根路径。
- 编码助手请先读 `docs/PROJECT_OVERVIEW.md`，并按 `AGENTS.md` 的阅读约定执行，避免整份加载长文档。
- 高危工具的参数会显示在确认卡片上；不要把密钥写入命令参数。
- 提交前建议检查：`git status` 中是否误带 `logs/`、`.env`、`__pycache__/`。
