# 项目速览（1 页版）

> 用途：给人和 AI 的快速上下文。命令与环境见 [README](../README.md)；深入设计见 [ARCHITECTURE.md](ARCHITECTURE.md)。
>
> 代码基线：`2026-10-27` 工作区快照（含小企业版 P0 守卫层）。
>
> 配套图：[architecture.svg](architecture.svg)（分层架构）；AI 阅读约定见 [AGENTS.md](../AGENTS.md)。
>
> 当前工具、动作、用例、预算、模型等数字以 [_generated/facts.md](_generated/facts.md) 为准；本文只保留结构、流程与“为什么”。

**本文目录**：[1 一句话](#1-一句话) · [2 技术栈](#2-技术栈) · [3 分层结构](#3-分层结构) · [4 请求流程](#4-一次请求怎么走) · [5 电脑操作与确认](#5-电脑操作与人工确认) · [6 目录](#6-目录速查) · [7 约束](#7-关键约束) · [8 问题索引](#8-按问题找位置) · [9 AI 约定](#9-给-ai-的阅读约定)

## 1. 一句话

基于 **LangChain 1.4 `create_agent`（LangGraph）** 的本地电脑助手原型：Streamlit UI + 工具调用 + 文件系统/进程/命令操作 + 截屏与鼠标键盘/窗口控制 + 高危操作人工确认，支持短期/本地 JSON/PostgreSQL 三种会话记忆；并接入小企业版 P0 守卫层（默认自动任务卡、可选机器判据、任务技能库、只读、批次回滚、预算急停、审计回放、单任务锁、PIN）。

## 2. 技术栈

| 组件 | 版本 | 作用 |
| --- | --- | --- |
| langchain / langgraph | 1.4.0 / 1.2.11 | Agent 编排与图执行 |
| langgraph-checkpoint-postgres / psycopg | 3.2.0 / 3.3.6 | 会话记忆：内存（默认）/ 本地 JSON / PostgreSQL |
| langchain-openai | 1.6.6 | `init_chat_model` 构造的 `ChatOpenAI` 调用 DashScope 兼容端点 |
| psutil | 7.2.2 | 系统信息、磁盘、进程枚举 |
| streamlit | 1.64.0 | 对话 UI 与流式渲染 |

模型：`qwen3.5-27b`（对话）、`qwen3.5-flash`（备用对话模型）。

## 3. 分层结构

| 层 | 关键组件 | 职责 |
| --- | --- | --- |
| 表现层 | `app.py` | 收集输入、展示流式输出、渲染高危操作确认卡片 |
| 编排层 | `ReactAgent` + 中间件 | 组装模型/工具/提示词，驱动 ReAct 循环，兜底工具异常与结果校验，历史截图只保留最近几张再发模型，拦截高危操作 |
| 守卫层 | `agent/guard/` + `agent/skill_library.py` | 任务卡（含一连串要求的子目标拆分）、可选机器判据、只读、R0–R3 分级、批次回滚、回滚边界、预算急停、原地打转拦截、审计、单任务锁、PIN 验签、任务技能库 |
| 工具层 | `os_tools`（13 个）+ `agent_tools`（7 个）+ `computer_use`（1 个，26 动作） | 本机信息、文件读写/搜索、进程、命令、业务查询、桌面控制（含控件级 UIA 与 OCR） |
| 服务层 | `PromptLoader`、`HumanInTheLoopMiddleware` | 提示词加载、人工审批 |
| 模型/存储 | `ChatOpenAI`、记忆后端、本地文件系统 | 推理、上下文持久化、文件读写 |
| 横切 | `config/`、`prompts/`、`utils/`、`logs/` | 配置、提示词、路径、日志、环境补丁 |

## 4. 一次请求怎么走

1. `app.py` 接收输入；先按目标从技能库**匹配已有的子技能**（默认开，可在侧边栏关）；默认按输入自动生成**快速任务卡**（目标=输入，预算用默认值），命中技能时把它的步骤/建议动作/核对项作为参考；需要时才打开高级设置；工作区、只读、人工确认和 PIN 仍然生效。
2. `ReactAgent.execute_stream(query, thread_id, task_card)` 先取单任务文件锁，再创建任务运行时（批次号、预算、任务卡快照），然后用 `resilience.repair_thread_state` 修复上次中断留下的悬空工具调用。
3. `create_agent` 构建 LangGraph；中间件顺序：模型降级 → `guard_model`（预算/急停短路）→ `trim_screenshot_history`（只把最近几张截图发给模型）→ PII → 错误兜底/重试 → `monitor_tool`（审计）→ `guard_preflight`（只读/任务卡/预算）→ HITL → `guard_execute`（确认后计步 + R2/R3 验签 + 原地打转拦截）→ 工具。
4. 模型返回 `tool_calls`：只读工具直接执行；工具异常先按规则重试，重试耗尽后转成 `status=error` 的 `ToolMessage`；文件类操作在写审计前做结果校验；命中 `CONFIRM_TOOLS` 的工具被 `HumanInTheLoopMiddleware` 挂起；R2/R3 动作在确认卡片上还要求输入审批口令（PIN）。
5. 挂起时 `ReactAgent` 产出 `AgentEvent(type="interrupt")`（带批次号与任务卡快照），`app.py` 渲染确认卡片；用户批准后用 `Command(resume=...)` 继续，恢复时沿用同一批次与预算。
6. 循环直到模型返回最终 `AIMessage`；`ReactAgent` 产出 `verification` 事件执行任务卡机器判据，`app.py` 用 `_render_events` 渲染文本；每次写操作都记入批次，可在侧边栏「批次与回滚」整批撤销。

## 5. 电脑操作与安全模型

**工作区域**：`config/agent.yml` 的 `workspace_roots` 决定所有路径操作的边界（默认用户目录 + 项目根）；敏感目录（`.ssh`、`.aws` 等）一律拒绝。读取、搜索、写入、删除、命令 `cwd` 都受限。界面保存工作区域/只读开关时只定点替换对应配置块（`agent/guard/config.py` 的 `update_agent_config_block`），保留注释与 `sensitive_dirs` 等其它配置。

**只读工具（直接执行）**：`list_directory`、`read_text_file`、`search_files`、`search_text`，模型调用后立即执行，但仍受工作区限制。

**敏感读取（需确认）**：`get_system_info`、`list_processes`，会读取设备标识或正在使用的软件。

**修改类工具（需确认）**：`write_text_file`、`create_directory`、`move_path`、`copy_path`、`delete_path`、`run_command`、`open_path`，统一在 `CONFIRM_TOOLS` 注册，由 `HumanInTheLoopMiddleware` 拦截。确认卡片用大白话说明“做什么、目标、风险、影响范围”。

**桌面控制（computer_use，单工具多动作）**：首选“控件级”操作——Windows 自带 UIA 能直接拿到控件名字/类型/位置（`ui_windows`/`ui_tree`/`ui_click`/`ui_set_text`），自带 OCR 能拿到屏幕文字与中心点（`ocr_text`/`click_text`），两者都由 `agent/tools/uia_bridge.ps1` 调系统接口，**不装任何新依赖**；控件树为空（自绘界面）才退回 OCR，坐标点击（OmniPilot 的 click/type_text）是最后选择。`ui_click`/`ui_set_text` 会在动手**之前**检查目标窗口：终端、脚本宿主、注册表/磁盘工具、系统关键进程、助手自身窗口、最小化窗口一律拒绝，密码框不读不写。截图以内容块回传，`app_open` 拒绝终端/脚本宿主/注册表/磁盘工具（含中英文显示名），Windows 下打不开时改用 `app_open_start_menu`。

**自动保险**：删除默认移到 `.agent_trash`；覆盖前备份到 `.agent_backup`；可执行后缀拒绝写入（复制/移动目标与目录树也检查）；29 条危险命令黑名单（含环境变量、编码命令、`cd ..`、下载/执行工具等绕过路径）；命令中的越界路径拒绝；项目源码/配置/提示词/脚本/日志/虚拟环境/Git 元数据/技能库拒绝 Agent 写入；任务卡预算（普通任务 30 步/10 分钟/2 元，要动鼠标键盘的桌面任务 60 步/20 分钟/5 元）+ 100 步/单次运行硬上限；同一区域反复同一动作会被 `repeat_guard` 先提醒后拦截（第 3 次提醒、第 6 次拒绝执行）。

**守卫层（小企业版 P0）**：界面自动生成快速任务卡，`start_task` 仍要求有卡；机器判据可选，填了才由代码核对，未填按人工确认验收；只读模式禁用全部写操作；每个任务一个批次号，可在侧边栏整批回滚，并明确命令/GUI/永久删除不在回滚范围；R2/R3 动作需要人工持有的 PIN，执行侧验签并绑定任务/批次/动作（凭证内存 + `.agent_state/approval_context.json` 落盘，避免 Streamlit 热重载后界面写新模块、执行端读旧模块）；急停写 `STATE_DIR/STOP` 文件；审计记录 `batch_id/step_no/reversibility/approver`；同一时刻只允许一个任务操作本机。

**失败与校验**：工具异常不打断循环（`ToolErrorMiddleware` → `status=error` 的 ToolMessage）；只对幂等/只读工具的瞬时错误（超时/断连/429/5xx）重试 2 次；写文件、建目录、移动、复制、删除在写审计前做确定性后置校验，校验不过就标 `error`；上次崩溃留下的悬空 `tool_call` 会在下次提问前自动补齐占位结果。

**操作留痕**：聊天页「本次操作记录」展示每一步的人话解释；`logs/audit_YYYYMMDD.jsonl` 记录结构化审计日志。

**报告场景**：模型调用 `fill_context_for_report` → `monitor_tool` 设置 `runtime.context["report"]=True` → 下一次模型调用由 `report_prompt_switch` 切换到 `report_prompt.txt`。标记只在本次请求内有效。

## 6. 目录速查

```text
agent/        Agent 组装、电脑工具 + 业务工具 + 桌面控制工具、守卫层、任务技能库、会话自愈、结果校验、安全策略与中间件
model/        Chat 模型工厂
utils/        配置、路径、日志、提示词加载
config/       model / agent / prompts
prompts/      main / report
data/         external/records.csv；task_skills/ 本地技能库（git 忽略）
memorys/      记忆方式工厂 + 短期/JSON/PostgreSQL checkpointer
tests/        unittest 用例（假模型，不联网）
docs/         架构文档、改造复盘、电脑操作说明、small-business-p0 与 SVG
env_patch/    .pth + sitecustomize.py
scripts/      apply_env_patch.py（环境补丁）+ verify_agent_resilience.py（离线验证+证据）+ verify_small_business.py（P0 验证+证据）+ setup_approval_pin.py
.agent_state/ 守卫层状态：批次/会话快照/锁/急停/审批上下文副本（git 忽略）
.vscode/      IDE 工作区配置（解释器 + 集成终端放行 Activate.ps1，随仓库生效）
```

## 7. 关键约束

- **默认短期记忆**：输入框左侧的记忆按钮可选短期（默认，不存档）/ 本地 JSON 存档 / PostgreSQL；侧边栏管理会话 ID。
- **工作区域是硬边界**：所有文件操作（包括读取）必须在 `workspace_roots` 内；敏感目录默认拒绝；可在输入框左侧「工作区域」按钮修改，保存后立即生效。
- **消息块级流式**：只输出不带 `tool_calls` 的 `AIMessage` 文本，工具中间消息不显示；操作记录单独展示。
- **高危操作必须确认**：确认卡片用大白话解释“做什么、目标、风险、影响范围”；R2/R3 动作还需输入审批口令（PIN），执行侧验签并校验任务/批次/动作；未设置 PIN 时批准按钮禁用。
- **可整批回滚**：一次任务一个批次号；写/移动/复制/删除/建目录/追加都记入 `.agent_state/batches/`，可在侧边栏一键撤销整批；永久删除会明确报“不可逆”。`run_command` 内部修改的文件、`computer_use` / `open_path` 的应用内状态不在回滚范围；任务卡与确认卡片会明确显示。
- **预算与急停是硬约束**：步数/时长/费用超限时 `guard_model` 直接短路，不再调用模型；急停写 `STOP` 文件，独立于模型。
- **任务卡默认自动**：界面按输入生成快速卡，`start_task` 仍要求有卡；机器判据可选，未配置时按人工确认验收。“打开微信，登录后给某人发个你好”这类一连串要求会被拆成子目标写进提示词，逐项推进；登录/密码/扫码/付款这类子目标会停下来交给人。技能库**一条技能 = 一个可复用子目标**（带匹配词/建议动作/核对项），下次描述同类任务会自动带上参考（可关），但只作提示、不自动执行。
- **单任务锁**：同一时刻只允许一个任务操作本机；等待人工确认的间隙会释放锁。
- **删除默认进回收区**：`.agent_trash` 可手动恢复；`permanent=True` 才永久删除。
- **命令检查不是沙箱**：黑名单 + 路径提取是尽力而为，确认卡片是最后防线。
- **桌面控制不是完整的 UI 自动化框架**：已接 Windows 自带控件树（UIA）与 OCR，但控件树有深度/数量上限、自绘界面只能靠 OCR、提权窗口被 UIPI 拦住；多显示器/DPI 缩放仍可能让坐标偏移。
- **截图仍会进入会话记忆**：base64 图片写入短期/JSON/PostgreSQL 记忆，会话仍会变大；现在只在发给模型时裁剪（保留最近 N 张），checkpoint 本身不回收。
- **确认链不能被自动化反用**：控件级动作拒绝终端/脚本宿主/注册表/系统关键进程/助手自身窗口/确认页所在浏览器，且“先检查后动手”；密码框不读不写。
- **桌面控制的应用边界**：`app_open` 不启动终端/脚本宿主/注册表/磁盘工具（避免绕过 `run_command`）；`app_close` 不关闭系统关键进程。
- **外部 CSV 为空**：`data/external/records.csv` 当前 0 字节；`fetch_external_data` 注解为 `str`、实际返回 `dict`。
- **Windows 集成终端已放宽执行策略**：`.vscode/settings.json` 给终端的 PowerShell 加了 `-ExecutionPolicy Bypass`（仅该终端进程、不写注册表），用于让自动执行的 `Activate.ps1` 跟 Windows 默认的 `Restricted` 共存；它不是安全防线，项目边界仍在 `safety.py` + HITL 确认 + 审计日志。
- **未实现**：鉴权、限流、多租户、文件上传 UI；已有离线用例（用例数与 P0 数见 [_generated/facts.md](_generated/facts.md)）；PII 仅覆盖模型输入/输出，日志中仅 `computer_use` 做了定向脱敏，其他工具参数仍未脱敏；`.env`/密钥文件读取仍未拦截（计划默认拒绝 + 独立敏感读取审批）；技能库截图可能含隐私，保存前需人工确认。

## 8. 按问题找位置

| 我想知道 | 看这里 |
| --- | --- |
| 项目怎么跑起来？ | [README 快速开始](../README.md#4-快速开始) |
| 整体分几层、用了什么？ | [ARCHITECTURE 第 4 节](ARCHITECTURE.md#4-总体架构) + [architecture.svg](architecture.svg) |
| 当前工具/动作/用例/预算/模型是多少？ | [_generated/facts.md](_generated/facts.md) |
| 一次请求怎么走？ | [ARCHITECTURE 第 6 节](ARCHITECTURE.md#6-运行时主链路) |
| 电脑操作与确认怎么实现？ | [ARCHITECTURE 第 7 节](ARCHITECTURE.md#7-电脑操作子系统) |
| 任务卡/批次回滚/预算急停/PIN 怎么实现？ | [小企业版 P0 进度备忘录](small-business-p0/00-进度备忘录.md) + 01–10 入门讲解 |
| 任务卡怎么简化、技能库怎么用？ | [10-任务卡简化与技能库](small-business-p0/10-任务卡简化与技能库.md) |
| P0 怎么验收、证据在哪？ | [08-交付复盘与验收清单](small-business-p0/08-交付复盘与验收清单.md) + `docs/small-business-p0/evidence/` |
| 电脑操作有哪些功能、怎么确认？ | [OS_TOOLS.md](OS_TOOLS.md) |
| 怎么让模型操作某个软件的界面（控件/OCR）？ | [OS_TOOLS 5.13 控件级操作](OS_TOOLS.md) + [ARCHITECTURE 7.6](ARCHITECTURE.md) |
| 工具失败/重试/结果校验怎么工作？ | [agent-resilience/01-入门讲解-失败重试与结果验证.md](agent-resilience/01-入门讲解-失败重试与结果验证.md) |
| 怎么验证改动没退化？ | [agent-resilience/02-验证与交付复盘.md](agent-resilience/02-验证与交付复盘.md) + `scripts/verify_agent_resilience.py`；P0 用 [08-交付复盘](small-business-p0/08-交付复盘与验收清单.md) + `scripts/verify_small_business.py` |
| 长任务改造的方法论？ | [06-长期改造经验与方法论.md](06-长期改造经验与方法论.md) |
| 桌面控制（截图/键鼠/应用）怎么用？ | [OS_TOOLS.md](OS_TOOLS.md) 第 5.8–5.12 节 + [ARCHITECTURE 第 7 节](ARCHITECTURE.md#7-电脑操作子系统与安全模型) |
| 安全能力、威胁模型、技术债与改进路线？ | [SECURITY_REPORT.md](SECURITY_REPORT.md)（2026-10-26 基线） |
| 当前程序到底安全到什么程度、还有哪些真实缺口？ | [SECURITY_REPORT_2026-10-27.md](SECURITY_REPORT_2026-10-27.md) |
| 怎么把 Agent 做得更安全可控？ | [AGENT_SECURITY_PLAYBOOK.md](AGENT_SECURITY_PLAYBOOK.md) |
| OmniPilot 桌面控制怎么接入、怎么用、有什么风险？ | [OMNIPILOT_INTEGRATION_REPORT.md](OMNIPILOT_INTEGRATION_REPORT.md) |
| 报告场景怎么切换？ | [ARCHITECTURE 第 8 节](ARCHITECTURE.md#8-报告场景与动态提示词) |
| 配置在哪改？ | [ARCHITECTURE 第 9 节](ARCHITECTURE.md#9-配置体系) + [README 配置速查](../README.md#7-配置入口速查) |
| 新建终端报「禁止运行脚本」/ IDE 终端怎么配？ | [README 4.2.1](../README.md#421-windows-新建终端报禁止运行脚本) + [ARCHITECTURE 9.9](ARCHITECTURE.md#99-ide-层配置与集成终端启动参数) |
| `uv sync` 后补丁丢了？ | [README 4.2](../README.md#42-安装依赖) + `uv run python scripts/apply_env_patch.py` |
| 想加工具/上传/记忆？ | [ARCHITECTURE 第 13 节](ARCHITECTURE.md#13-扩展指南) |
| 有什么坑/技术债？ | [ARCHITECTURE 第 12 节](ARCHITECTURE.md#12-已知问题与技术债) |
| 当前项目有什么不足、限制、取舍和建议？ | [项目现状审查报告-2026-10-28.md](项目现状审查报告-2026-10-28.md) |
| 启动失败/日志在哪？ | [ARCHITECTURE 第 11 节](ARCHITECTURE.md#11-可观测性与错误处理) + [16.2](ARCHITECTURE.md#162-常用命令) |
| 代码文件对应关系？ | [ARCHITECTURE 16.1](ARCHITECTURE.md#161-关键文件索引) |
| 最近改了什么、为什么改？ | [CHANGELOG](../CHANGELOG.md) |

## 9. 给 AI 的阅读约定

完整约定见 [AGENTS.md](../AGENTS.md)。核心四点：

- 先读本文件，再按第 8 节索引只读 `ARCHITECTURE.md` 对应章节，不要整份加载。
- 不要为了建立认知去整份读 `docs/*.svg`；图是给人看的，文字模块表更适合 AI（改图时只 grep `<text>` 节点定位）。
- 代码是最终事实源；文档与代码冲突时，以代码为准并更新文档。
- 增量变更写 [CHANGELOG.md](../CHANGELOG.md)，不要塞进架构正文。
