# 本地电脑助手 Agent

基于 **LangChain 1.x `create_agent`（LangGraph）** 构建的工具调用型本地电脑助手：支持系统信息查询、文件读写与搜索、进程查看、命令执行（需确认）、截屏与鼠标键盘/窗口控制（需确认），以及 Streamlit 对话界面。

> 小企业版 P0 守卫层已接入：任务卡、只读总开关、批次整批回滚、预算急停、审计回放、单任务锁、第二主体审批口令（PIN）。入口文档见 [docs/small-business-p0/00-进度备忘录.md](docs/small-business-p0/00-进度备忘录.md)。
>
> 这是一份面向开发者的项目入口文档。5 分钟速览见 [docs/PROJECT_OVERVIEW.md](docs/PROJECT_OVERVIEW.md)；深入原理、配置项、扩展步骤与技术债见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 导读

**先选一条路线**

| 目标 | 建议阅读 | 时间 |
| --- | --- | --- |
| 5 分钟知道项目是什么 | 本文第 1–2 节 + [1 页速览](docs/PROJECT_OVERVIEW.md) | 5 分钟 |
| 把项目跑起来 | 本文第 4 节 + 第 7 节 | 15 分钟 |
| 改代码/加功能 | [ARCHITECTURE](docs/ARCHITECTURE.md) 第 5、6、13 节 | 30–60 分钟 |
| 想懂“工具失败为什么不再停”、或要跑回归验证 | [入门讲解](docs/agent-resilience/01-入门讲解-失败重试与结果验证.md) + [验证复盘](docs/agent-resilience/02-验证与交付复盘.md) | 20–30 分钟 |
| 想懂任务卡/批次回滚/预算急停/PIN 怎么落地 | [小企业版 P0 进度备忘录](docs/small-business-p0/00-进度备忘录.md) + 对应 01–08 入门讲解 | 30–60 分钟 |
| 排障/上线 | [ARCHITECTURE](docs/ARCHITECTURE.md) 第 10、11、12 节 | 按需 |

**文档地图**

| 文件 | 用途 |
| --- | --- |
| [README.md](README.md) | 当前文件：门面、上手指南、命令与配置速查 |
| [docs/PROJECT_OVERVIEW.md](docs/PROJECT_OVERVIEW.md) | 1 页速览，人和 AI 的快速入口 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 当前状态的深度参考：模块、流程、配置、扩展、技术债 |
| [docs/architecture.svg](docs/architecture.svg) | 分层架构图（人看） |
| [docs/OS_TOOLS.md](docs/OS_TOOLS.md) | 电脑操作功能与安全说明（普通人向 + 开发者参数） |
| [docs/SECURITY_REPORT.md](docs/SECURITY_REPORT.md) | 安全评估报告：能力矩阵、威胁模型、技术债与改进路线 |
| [docs/AGENT_SECURITY_PLAYBOOK.md](docs/AGENT_SECURITY_PLAYBOOK.md) | Agent 安全实战经验：六层防御、十二条经验、HITL 分级、提示词注入、反模式、30/60/90 路线 |
| [docs/OMNIPILOT_INTEGRATION_REPORT.md](docs/OMNIPILOT_INTEGRATION_REPORT.md) | OmniPilot 桌面控制接入报告（小白版）：做了什么、怎么接入、怎么用、不能怎么用、风险 |
| [docs/small-business-p0/](docs/small-business-p0/00-进度备忘录.md) | 小企业版 P0：任务卡、只读、批次回滚、预算急停、审计回放、单任务锁、第二主体（入门讲解 + 验收复盘） |
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
| 业务形态 | 本地电脑助手：查看系统与进程、浏览/搜索/读写文件、执行命令、截屏并控制鼠标键盘/应用，并保留原有业务查询与报告能力 |
| 技术形态 | 单进程 Streamlit 应用 + LangGraph Agent + 本地文件系统/psutil + OmniPilot 工具层 |
| 适用范围 | 原型验证、LangChain Agent 学习、个人效率工具 |
| 非目标 | 生产级多租户、鉴权、计费、高可用；当前均未实现 |

**一句话架构**：Streamlit 收集指令 → `ReactAgent` 调用 `create_agent` 构建 LangGraph → 模型在中间件约束下循环调用工具 → 只读工具直接执行、高危工具等待用户确认 → 模型生成答案 → 按消息块流式返回页面。

![系统架构总览](docs/architecture.svg)

---

## 2. 能力清单

| 能力 | 状态 | 实现位置 |
| --- | --- | --- |
| 多轮工具调用（ReAct 循环） | ✅ 已接入 | `agent/react_agent.py` → `create_agent` |
| 工具异常不中断循环 | ✅ 已接入 | `agent/tools/middleware.py` → `tool_error_middleware`（异常转 `status=error` 的 ToolMessage） |
| 幂等工具有限重试 | ✅ 已接入 | `agent/tools/middleware.py` → `tool_retry_middleware`（仅只读工具；超时/断连/429/5xx） |
| 文件操作结果校验 | ✅ 已接入 | `agent/tools/verification.py` → 写/建目录/移动/复制/删除的后置核对；`run_command` 校验退出码；桌面动作标“未确认” |
| 会话自愈（悬空 tool_call） | ✅ 已接入 | `agent/resilience.py` + `ReactAgent.execute_stream` |
| 系统信息 / 磁盘 / 进程查看 | ✅ 已接入（需确认） | `agent/tools/os_tools.py` → `get_system_info`、`list_processes` |
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
| 单轮工具调用上限 | ✅ 已接入 | 任务卡预算（默认 30 步 / 10 分钟 / 2 元）+ `ToolCallLimitMiddleware` 100 步硬上限 |
| 任务卡（目标/判据/范围/预算） | ✅ 已接入 | `agent/guard/task_card.py` + `app.py` 侧边栏；缺字段不允许启动 |
| 只读总开关 | ✅ 已接入 | `config/agent.yml` 的 `read_only` + 侧边栏开关 + `agent/guard/policy.py` |
| 批次号 + 整批回滚 | ✅ 已接入 | `agent/guard/journal.py` + `.agent_state/batches/` + 侧边栏「批次与回滚」 |
| 预算与急停 | ✅ 已接入 | `agent/guard/state.py`、`agent/guard/middlewares.py`；`STOP` 文件与界面急停按钮 |
| 第二主体审批口令（PIN） | ✅ 已接入 | `agent/guard/approval.py` + `scripts/setup_approval_pin.py`；R2/R3 动作需要 |
| 审计与回放 | ✅ 已接入 | `utils/audit_logger.py` 扩展字段 + 批次回放 UI |
| 单任务全局锁 | ✅ 已接入 | `agent/guard/lock.py` + `ReactAgent` 的 `task_lock` |
| 工具调用日志 | ✅ 已接入 | `agent/tools/middleware.py` → `monitor_tool` |
| 离线验证用例 | ✅ 已接入 | `tests/`（`unittest`）+ `scripts/verify_agent_resilience.py` + `scripts/verify_small_business.py` |
| 模型调用前状态日志 | ✅ 已接入 | `agent/tools/middleware.py` → `log_before_model` |
| 报告场景动态提示词 | ✅ 已接入 | `fill_context_for_report` + `report_prompt_switch` |
| 联网搜索（百度） | ✅ 已接入 | `agent/tools/agent_tools.py` → `web_search_baidu` |
| PII 脱敏（邮箱/手机号） | ✅ 已接入 | `agent/tools/middleware.py` → `PIIMiddleware` |
| 模型降级（主模型 → 备用模型） | ✅ 已接入 | `agent/tools/middleware.py` → `ModelFallbackMiddleware` |
| 外部使用记录查询 | 🧪 示例 | `fetch_external_data` 读取 CSV，示例文件当前为空 |
| 天气 / 城市 / 用户 ID / 月份 | 🧪 模拟 | `agent/tools/agent_tools.py` 中返回随机或固定值 |
| 会话记忆（短期 / 本地 JSON / PostgreSQL） | ✅ 已接入 | `app.py` 输入框左侧按钮切换；`memorys/memory_factory.py`、`file_checkpointer.py`、`postgres_checkpointer.py` |
| 文件上传 UI | 🧪 预留 | `app_file_uploader.py` 当前为空壳 |
| 桌面控制（截图 / 鼠标 / 键盘 / 剪贴板 / 应用） | ✅ 已接入（除只读动作外需确认） | `agent/tools/computer_use.py`（复用 OmniPilot 工具层） |
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
| 桌面控制 | omnipilot-agent[gui] | 0.1.0 | 复用其 screen/gui/clipboard/apps 工具层（pyautogui + Pillow） |
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

> `uv sync` 每次重建 `.venv` 都会清空 `site-packages`，虚拟环境补丁因此丢失。重建后执行一次即可自动补回（幂等，可反复运行）：
>
> ```powershell
> uv run python scripts/apply_env_patch.py
> ```
>
> 脚本把 `env_patch/` 下的 `.pth` 与 `sitecustomize.py` 装进当前解释器的 `site-packages`：目标路径由解释器自报（Windows/Linux/macOS 通用，不再写死 `Lib`），`.pth` 里的相对路径会改写成绝对路径。补丁缺失时的症状：IDE 里直接运行 `.py` 报 `ModuleNotFoundError`，Windows 终端出现中文/emoji 编码错误。检查是否已生效用 `uv run python scripts/apply_env_patch.py --check`（未生效退出码 1）。用 `uv run -m ...` 从项目根运行可规避导入问题。

### 4.2.1 Windows 新建终端报「禁止运行脚本」

现象：新建终端出现 `无法加载文件 ....venv\Scripts\Activate.ps1，因为在此系统上禁止运行脚本`（`UnauthorizedAccess`）。

原因链：`.vscode/settings.json` 把解释器钉在 `.venv`，Python 扩展因此**每次新建终端都自动执行** `Activate.ps1`；而 Windows 家用版 PowerShell 的执行策略默认是 `Restricted`（拦住所有 `.ps1` 脚本文件）。**真正的代价不是那行红字，而是虚拟环境没有激活**：`python`/`pip` 指向全局解释器（`pip install` 会装错地方），`env_patch` 的路径与编码补丁也一并失效。

本项目已在 `.vscode/settings.json` 里给集成终端的 PowerShell 配置加上 `-ExecutionPolicy Bypass`：只作用于从本工作区打开的**那个终端进程**，不写注册表、不改用户/机器策略，窗口关闭即失效。**这条配置会随仓库对所有协作者生效，属于进程级放宽，请知悉**（取舍与边界见 [ARCHITECTURE 9.9](docs/ARCHITECTURE.md#99-ide-层配置与集成终端启动参数)）。

仍报错时按顺序排查：

1. **重开终端**：执行策略在 PowerShell 进程启动时读取，配置前已打开的窗口不会变化。
2. 确认终端确实由本工作区打开（换个文件夹打开项目则不套用该配置）。
3. 想让 IDE 之外的 PowerShell 也正常，本机执行一次（只影响当前 Windows 账户，保留「下载脚本需签名」的底线）：

   ```powershell
   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
   ```

   查看用 `Get-ExecutionPolicy`（最终生效值）与 `Get-ExecutionPolicy -List`（各作用域）；撤回用 `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy Undefined`。
4. 不想改任何策略：用 `uv run ...`（uv 自己定位 `.venv`，不需要激活），或把默认终端换成 cmd / Git Bash（它们走 `activate.bat` / `activate`，不受执行策略约束）。

自检（唯一可靠判据，**不要只看 IDE 右下角的解释器标签**，它不代表终端已激活）：

```powershell
python -c "import sys; print(sys.executable)"   # 应输出 <项目根>\.venv\Scripts\python.exe
```

### 4.3 准备 `.env`

在项目根目录创建 `.env`（该文件已被 `.gitignore` 忽略，不要提交）：

| 变量名 | 使用方 | 是否必需 |
| --- | --- | --- |
| `OPEN_API_KEY` | `model/factory.py` 中 `init_chat_model(api_key=...)` | 是（注意变量名不是 `OPENAI_API_KEY`） |
| `BASE_URL_DASHSCOPE` | `init_chat_model(base_url=...)` 的 OpenAI 兼容端点 | 是 |
| `DB_URL_POSTGRES` | `memorys/postgres_checkpointer.py` | 否；在 Streamlit 选择 PostgreSQL 记忆方式时必需 |
| `USE_POSTGRES_CHECKPOINTER` | `agent/react_agent.py` | 否；兼容配置，未显式传入 `memory_mode` 时默认使用 PostgreSQL |
| `API_KEY_BAIDU` | `web_search_baidu` | 否；不配置时工具返回空字符串 |
| `OMNIPILOT_HOME` | `agent/tools/computer_use.py` → OmniPilot 工具层 | 否；不设置时默认写到项目 `logs/omnipilot`（截图/会话目录） |
| `BASE_URL_DEEPSEEK`、`API_KEY_DEEPSEEK`、`API_HOST_DASHSCOPE` | 当前代码未使用 | 否，预留 |

### 4.4 启动对话界面

```powershell
uv run streamlit run app.py
```

浏览器打开 Streamlit 输出的地址即可对话。**对话输入框左侧**有两个按钮：

- **记忆按钮**：切换会话记忆方式（下方详述）。
- **工作区域按钮**：查看和修改允许操作的目录，每行一个绝对路径，点「保存并应用」后立即生效，并写回 `config/agent.yml`。

记忆方式的说明：

- **短期记忆（默认，不存档记忆）**：仅在当前进程内按会话 ID 保留上下文，刷新页面或重启即丢失。
- **本地记忆（以JSON格式本地存档）**：写入 `memorys/chat_history/agent_checkpoints.json`，重启后可恢复。
- **PostgreSQL（存档入数据库，需配置）**：使用 `.env` 的 `DB_URL_POSTGRES`，支持跨进程/多实例恢复。

弹层底部显示当前方式的存储位置；左侧栏「会话管理」提供会话 ID、新建会话与清空记忆（后续可在此列出不同会话记录）。相同会话 ID 即可恢复对应历史。

### 4.5 常用自检命令

```powershell
uv run -m agent.react_agent      # 直接跑一次 Agent（会真实调用模型）
uv run -m agent.tools.os_tools   # 验证电脑工具（不调用模型）
uv run -m agent.tools.computer_use  # 只读桌面工具冒烟（屏幕尺寸/应用列表/等待）
uv run -m utils.logger_handler   # 验证日志输出
uv run python scripts/apply_env_patch.py --check  # 验证虚拟环境补丁是否已生效
.venv\Scripts\python.exe scripts\verify_agent_resilience.py  # 跑离线验证用例并生成证据 JSON
uv run python scripts/verify_small_business.py  # 跑小企业版 P0 验证并生成证据 JSON
uv run python scripts/setup_approval_pin.py --status  # 查看审批口令是否已设置
```

> `agent.react_agent` 会触发真实模型调用，运行前确认 `.env` 已配置；其余几条都不调用模型（验证脚本用假模型）。

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
├── .vscode/settings.json       # ⚙️ IDE 配置：解释器 + 集成终端 Bypass（git 跟踪，见 4.2.1）
├── agent/
│   ├── react_agent.py          # ✅ Agent 组装、HITL 确认、事件流、会话自愈、单任务锁入口
│   ├── resilience.py           # ✅ 悬空 tool_call 的识别与修复
│   ├── guard/                  # ✅ 小企业版 P0 守卫层
│   │   ├── task_card.py        # ✅ 任务卡结构与校验
│   │   ├── policy.py           # ✅ 只读 / 任务卡范围 / R0–R3 分级
│   │   ├── state.py            # ✅ 预算 / 急停 / 会话快照 / 审批上下文
│   │   ├── journal.py          # ✅ 批次日志与整批回滚
│   │   ├── lock.py             # ✅ 单任务文件锁
│   │   ├── approval.py         # ✅ PIN 哈希 / HMAC 凭证 / 审批目录保护
│   │   └── middlewares.py      # ✅ 守卫层中间件
│   └── tools/
│       ├── os_tools.py         # ✅ 13 个电脑信息与文件操作工具
│       ├── computer_use.py     # ✅ 1 个桌面控制工具（18 个动作，复用 OmniPilot）
│       ├── safety.py           # ✅ 工作区域、命令拦截、备份、回收区
│       ├── verification.py     # ✅ 文件操作结果的后置校验
│       ├── agent_tools.py      # ✅ 7 个业务工具
│       └── middleware.py       # ✅ 8 个中间件（含错误兜底、有限重试）
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
│   ├── logger_handler.py       # ⚙️ 控制台 + 按日文件日志
│   └── audit_logger.py         # ⚙️ 工具调用审计日志
├── config/                     # ⚙️ model / prompts / agent 配置
├── prompts/                    # ⚙️ 系统提示词、报告提示词
├── data/                       # external/ 下为业务 CSV
├── logs/                       # 按日日志
├── env_patch/                  # ⚙️ 虚拟环境补丁（.pth + sitecustomize.py）
├── scripts/
│   ├── apply_env_patch.py      # ⚙️ 把 env_patch 装进 .venv（每次 uv sync 后执行）
│   ├── verify_agent_resilience.py  # ✅ 跑离线用例并生成证据快照
│   ├── verify_small_business.py    # ✅ 跑小企业版 P0 用例并生成证据
│   └── setup_approval_pin.py       # ⚙️ 设置/查看审批口令（PIN）
├── .agent_state/              # ⚙️ 守卫层状态：批次/会话快照/锁/急停（git 忽略）
├── tests/                      # ✅ unittest 用例（假模型，不联网）
└── docs/                       # 速览、架构文档、功能说明、改造复盘与 SVG 图
```

图例：✅ 主链路 / 🧪 示例或预留 / ⚙️ 基础设施。

---

## 6. 代码阅读路线

约 30 分钟，按依赖顺序阅读：

1. `app.py`：看 UI 如何创建 Agent、如何接收流式输出。
2. `agent/react_agent.py`：看 `create_agent` 的模型、工具、中间件三要素。
3. `agent/tools/safety.py`：看工作区域、命令拦截、备份、回收区和小白解释。
4. `agent/tools/os_tools.py`：看电脑信息、文件读写、搜索与命令执行如何用 `@tool` 描述，以及路径/系统目录保护。
5. `agent/tools/computer_use.py`：看单个工具如何按 `action` 分发到 OmniPilot 的 screen/gui/clipboard/apps，以及哪些动作直接执行、哪些需要确认。
6. `agent/tools/agent_tools.py`：看原有业务工具与 CSV 懒加载。
7. `agent/tools/middleware.py`：看工具监控、模型前日志、动态提示词切换、PII 脱敏、模型降级，以及错误兜底与有限重试。
8. `agent/resilience.py` + `agent/tools/verification.py`：看中断后如何自愈、工具结果如何被确定性核对。
9. `agent/react_agent.py` 的 `CONFIRM_TOOLS` / `hitl_middleware`：看哪些工具需要人工确认。
10. `model/factory.py` → `config/*.yml`：看模型与参数如何装配。
11. `utils/*`：看配置、路径、日志、提示词加载的公共约定。

更完整的调用时序、配置表、扩展步骤和已知问题见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 7. 配置入口速查

| 想改什么 | 改哪里 |
| --- | --- |
| 换对话模型 | `config/model.yml` + `model/factory.py` |
| 设置工作区域 | 输入框左侧「工作区域」按钮（立即生效），或 `config/agent.yml` 的 `workspace_roots`（重启生效） |
| 设置敏感目录 | `config/agent.yml` 的 `sensitive_dirs` |
| 调整备份/回收区/命令黑名单 | `agent/tools/safety.py` |
| 调整桌面控制的直接执行/确认范围 | `agent/tools/computer_use.py` 的 `READ_ONLY_ACTIONS`；确认策略在 `agent/react_agent.py` 的 `_HITL_CONFIG` |
| 调整桌面控制的应用/进程黑名单 | `agent/tools/computer_use.py` 的 `_BLOCKED_APPS` / `_PROTECTED_PROCESSES` |
| 设置备用对话模型 | `config/model.yml` 的 `backup_chat_model_name` |
| 调整命令超时/读取上限 | `agent/tools/os_tools.py` 顶部常量 |
| 换外部 CSV 路径 | `config/agent.yml` |
| 换提示词 | `prompts/*.txt`（Agent 系统提示词每次模型调用前重读） |
| 新增工具 | `agent/tools/os_tools.py` + `agent/react_agent.py` 的 tools 列表；有副作用时同步加入 `CONFIRM_TOOLS` |
| 调整自动重试范围/条件 | `agent/tools/middleware.py` 的 `RETRYABLE_TOOLS` 与 `_should_retry_tool_error` |
| 调整结果校验范围 | `agent/tools/verification.py` 的 `_CHECKERS` 与 `_SUCCESS_PREFIXES` |
| 只读模式 | 侧边栏「安全开关」，或 `config/agent.yml` 的 `read_only`；临时演练用 `AGENT_READ_ONLY=1` |
| 任务预算默认值 | `config/agent.yml` 的 `task_defaults`（步数/分钟/费用） |
| 模型单价（费用估算） | `config/agent.yml` 的 `model_prices` |
| 急停 / 解除急停 | 侧边栏「安全开关」，或手动创建/删除 `.agent_state/STOP` |
| 设置审批口令（PIN） | `uv run python scripts/setup_approval_pin.py`（只存哈希） |
| 批次回滚 | 侧边栏「批次与回滚」；状态文件在 `.agent_state/batches/` |
| 新增场景 | 参考报告场景：工具 + 中间件标记 + 动态提示词 + prompt 文件 |
| 切换会话记忆方式 | Streamlit **输入框左侧的记忆按钮**（点击弹出列表）：短期记忆（默认，不存档记忆）/ 本地记忆（以JSON格式本地存档）/ PostgreSQL（存档入数据库，需配置） |
| 管理会话 ID / 新建会话 / 清空记忆 | Streamlit 左侧栏「会话管理」（后续可在此列出会话记录） |
| PostgreSQL 连接 | `.env` 的 `DB_URL_POSTGRES`；`USE_POSTGRES_CHECKPOINTER=true` 仅作为无 UI 场景的默认记忆方式 |
| 百度联网搜索 | `.env` 的 `API_KEY_BAIDU` |
| 补装/校验虚拟环境补丁 | `uv run python scripts/apply_env_patch.py`（加 `--check` 只校验），源文件在 `env_patch/` |
| 改 IDE 终端行为（含 Windows 执行策略放宽） | `.vscode/settings.json`（随仓库对所有协作者生效，见 4.2.1） |

---

## 8. 已知限制

先看这 5 条：

1. **默认是短期记忆**：进程内按会话 ID 保留上下文，刷新页面或重启即丢失；本地记忆/PostgreSQL 模式可跨进程恢复（PostgreSQL 需配置连接串）。
2. **流式输出是“消息块级”而非 token 级**：只输出 `AIMessage` 文本，用户问题与工具中间结果不回显；UI 按事件块渲染。
3. **PII 脱敏覆盖不完整**：`PIIMiddleware` 只处理模型输入/输出；日志中 `computer_use` 的 `type_text`/`clipboard_set` 内容已做定向脱敏，其他工具参数仍未脱敏。
4. **外部数据示例为空**：`data/external/records.csv` 为空，`fetch_external_data` 会查不到记录；CSV 解析也未处理带逗号的引号字段。
5. **命令检查不是沙箱**：`run_command` 使用黑名单和工作区路径检查，精心构造的命令仍可能绕过；确认卡片是最后一道防线。
6. **桌面控制不是 UI 自动化框架**：没有控件树/OCR，主要靠截图 + 坐标或模板匹配；多显示器、DPI 缩放或窗口移动可能导致坐标偏移。
7. **截图会进入会话记忆**：截图以 base64 形式写入短期/JSON/PostgreSQL 记忆，连续截图会让会话体积变大，当前没有自动清理。
8. **桌面控制禁止启动终端/脚本宿主**：`app_open` 拒绝 `cmd`、PowerShell、WSL、注册表、磁盘工具等，避免绕过 `run_command`；需要执行命令时走 `run_command`。
9. **工作区限制基于路径比较**：不防符号链接/junction 逃逸，`resolve()` 已尽量解析。
10. **界面保存会重写配置文件**：通过「工作区域」按钮保存时使用 `yaml.safe_dump`，`config/agent.yml` 中的注释会丢失。
11. **结果校验已补命令退出码，但仍不覆盖 GUI**：写文件、建目录、移动、复制、删除有确定性后置校验；`run_command` 校验退出码；`computer_use` / `open_path` 只能标“未确认”，需人看一眼。
12. **审批口令是“同机第二主体”**：能防误触和 Agent 自点确认，挡不住同一 Windows 用户下的恶意程序；需要带外审批时应升级方案。
13. **批次日志不是 WORM**：`.agent_state/` 对 Agent 工具禁写，但同用户的其他程序仍可改；只能防误操作，不防蓄意篡改。
14. **永久删除不可回滚**：批次回滚会明确报“不可逆”；P0 建议只用回收区删除。
15. **费用上限依赖 usage_metadata**：供应商不返回 usage 时只记录调用次数，不估算费用；此时依赖步数/时长上限。
16. **单任务锁是单机文件锁**：只对同一台机器、同一个状态目录生效；不是分布式锁。

完整清单与修复建议见 [docs/ARCHITECTURE.md#12-已知问题与技术债](docs/ARCHITECTURE.md#12-已知问题与技术债)。

---

## 9. 文档索引

| 文档 | 内容 |
| --- | --- |
| [docs/PROJECT_OVERVIEW.md](docs/PROJECT_OVERVIEW.md) | 1 页速览：定位、分层、主流程、关键约束、问题索引 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 架构、模块职责、调用时序、配置、扩展指南、技术债 |
| [docs/architecture.svg](docs/architecture.svg) | 分层架构总览图 |
| [docs/OS_TOOLS.md](docs/OS_TOOLS.md) | 电脑操作功能与安全说明：工作区域、确认卡片、回收区、审计日志、参数表 |
| [docs/SECURITY_REPORT.md](docs/SECURITY_REPORT.md) | 安全评估报告：会做/能做/不会做/不能做、措施实现、STRIDE/OWASP 映射、技术债、改进路线 |
| [docs/AGENT_SECURITY_PLAYBOOK.md](docs/AGENT_SECURITY_PLAYBOOK.md) | Agent 安全实战经验：六层防御、十二条经验、HITL 分级、提示词注入、反模式、30/60/90 路线 |
| [docs/OMNIPILOT_INTEGRATION_REPORT.md](docs/OMNIPILOT_INTEGRATION_REPORT.md) | OmniPilot 桌面控制接入报告（小白版）：做了什么、实现方法、怎么用、不能怎么用、风险 |
| [docs/small-business-p0/00-进度备忘录.md](docs/small-business-p0/00-进度备忘录.md) | 小企业版 P0 进度、P0 清单、验收方式、风险提醒 |
| [docs/small-business-p0/01-批次号与整批回滚.md](docs/small-business-p0/01-批次号与整批回滚.md) | 批次号、批次日志、整批回滚与边界 |
| [docs/small-business-p0/02-预算与急停.md](docs/small-business-p0/02-预算与急停.md) | 步数/时长/费用预算、急停、代码短路 |
| [docs/small-business-p0/03-任务卡与只读模式.md](docs/small-business-p0/03-任务卡与只读模式.md) | 任务卡字段、范围强制执行、只读总开关 |
| [docs/small-business-p0/04-确定性验证补齐.md](docs/small-business-p0/04-确定性验证补齐.md) | 退出码校验、未确认标记、假成功防线 |
| [docs/small-business-p0/05-审计与回放.md](docs/small-business-p0/05-审计与回放.md) | 审计三问、字段、事件、回放视图 |
| [docs/small-business-p0/06-单任务锁.md](docs/small-business-p0/06-单任务锁.md) | 单任务锁、过期清理、确认间隙取舍 |
| [docs/small-business-p0/07-第二主体审批口令.md](docs/small-business-p0/07-第二主体审批口令.md) | PIN 哈希、签名凭证、审批流程、威胁边界 |
| [docs/small-business-p0/08-交付复盘与验收清单.md](docs/small-business-p0/08-交付复盘与验收清单.md) | P0 验收对照、用例结果、反思、runbook、下一步 |
| [docs/06-长期改造经验与方法论.md](docs/06-长期改造经验与方法论.md) | 长任务改造方法论：任务契约、评估尺、阶段门、复盘与反幻觉 |
| [docs/agent-resilience/00-进度备忘录.md](docs/agent-resilience/00-进度备忘录.md) | 循环韧性改造的进度、决策与已知缺口 |
| [docs/agent-resilience/01-入门讲解-失败重试与结果验证.md](docs/agent-resilience/01-入门讲解-失败重试与结果验证.md) | 入门讲解：ReAct 循环、失败四根因、重试规则、结果校验、会话自愈 |
| [docs/agent-resilience/02-验证与交付复盘.md](docs/agent-resilience/02-验证与交付复盘.md) | 验证体系、用例结果、真实冒烟、阶段门与复盘 |
| [AGENTS.md](AGENTS.md) | AI 阅读约定（可选，供编码助手使用） |
| [CHANGELOG.md](CHANGELOG.md) | 重要变更：加了什么 / 改了什么 / 修了什么 |

---

## 10. 安全与协作约定

- `.env` 已加入 `.gitignore`；不要把 API Key、数据库连接串写进代码、提示词、日志或文档。
- `logs/` 会记录用户输入与工具参数，接入真实业务后需考虑脱敏与保留周期；目前仅图片 base64、桌面文字输入与剪贴板内容做了定向脱敏。
- `computer_use` 会把截图写到 `logs/omnipilot/screenshots/`，并把图片作为工具结果发给模型；截图可能包含密码、聊天记录等隐私内容，确认前请看清楚。
- 不要用 `computer_use` 输入密码/验证码，也不要在无人值守时自动代点确认；终端与脚本宿主已被 `app_open` 拒绝，需要命令时走 `run_command`。
- 新增入口脚本建议从项目根目录用 `uv run -m <module>` 运行；直接运行 `.py` 依赖 `env_patch` 注入的项目根路径（`uv sync` 重建环境后先执行 `uv run python scripts/apply_env_patch.py` 补回）。
- 编码助手请先读 `docs/PROJECT_OVERVIEW.md`，并按 `AGENTS.md` 的阅读约定执行，避免整份加载长文档。
- 高危工具的参数会显示在确认卡片上；不要把密钥写入命令参数。
- `.vscode/settings.json` 会随仓库改变协作者的 IDE 行为（当前包含集成终端的 `-ExecutionPolicy Bypass`）；改它要在评审里说明缘由，也不要往里面放机器级策略或个人偏好。
- 提交前建议检查：`git status` 中是否误带 `logs/`、`.env`、`__pycache__/`。
