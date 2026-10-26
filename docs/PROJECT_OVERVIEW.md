# 项目速览（1 页版）

> 用途：给人和 AI 的快速上下文。命令与环境见 [README](../README.md)；深入设计见 [ARCHITECTURE.md](ARCHITECTURE.md)。
>
> 代码基线：`2026-10-25` 工作区快照。
>
> 配套图：[architecture.svg](architecture.svg)（分层架构）；AI 阅读约定见 [AGENTS.md](../AGENTS.md)。

**本文目录**：[1 一句话](#1-一句话) · [2 技术栈](#2-技术栈) · [3 分层结构](#3-分层结构) · [4 请求流程](#4-一次请求怎么走) · [5 电脑操作与确认](#5-电脑操作与人工确认) · [6 目录](#6-目录速查) · [7 约束](#7-关键约束) · [8 问题索引](#8-按问题找位置) · [9 AI 约定](#9-给-ai-的阅读约定)

## 1. 一句话

基于 **LangChain 1.4 `create_agent`（LangGraph）** 的本地电脑助手原型：Streamlit UI + 工具调用 + 文件系统/进程/命令操作 + 高危操作人工确认，支持短期/本地 JSON/PostgreSQL 三种会话记忆。

## 2. 技术栈

| 组件 | 版本 | 作用 |
| --- | --- | --- |
| langchain / langgraph | 1.4.0 / 1.2.11 | Agent 编排与图执行 |
| langgraph-checkpoint-postgres / psycopg | 3.2.0 / 3.3.6 | 会话记忆：内存（默认）/ 本地 JSON / PostgreSQL |
| langchain-openai | 1.6.6 | `init_chat_model` 构造的 `ChatOpenAI` 调用 DashScope 兼容端点 |
| psutil | 7.2.2 | 系统信息、磁盘、进程枚举 |
| streamlit | 1.64.0 | 对话 UI 与流式渲染 |

模型：`qwen3.5-flash`（对话）、`glm-5`（备用对话模型）。

## 3. 分层结构

| 层 | 关键组件 | 职责 |
| --- | --- | --- |
| 表现层 | `app.py` | 收集输入、展示流式输出、渲染高危操作确认卡片 |
| 编排层 | `ReactAgent` + 8 个中间件 | 组装模型/工具/提示词，驱动 ReAct 循环，拦截高危操作 |
| 工具层 | `os_tools`（13 个）+ `agent_tools`（7 个） | 本机信息、文件读写/搜索、进程、命令、业务查询 |
| 服务层 | `PromptLoader`、`HumanInTheLoopMiddleware` | 提示词加载、人工审批 |
| 模型/存储 | `ChatOpenAI`、记忆后端、本地文件系统 | 推理、上下文持久化、文件读写 |
| 横切 | `config/`、`prompts/`、`utils/`、`logs/` | 配置、提示词、路径、日志、环境补丁 |

## 4. 一次请求怎么走

1. `app.py` 接收输入，调用 `ReactAgent.execute_stream(query, thread_id)`。
2. `create_agent` 构建 LangGraph，传入 `context={"report": False}`、`config={"thread_id": ...}`。
3. 中间件在模型调用外层做模型降级与 PII 脱敏，并在调用前记录日志、选择系统提示词。
4. 模型返回 `tool_calls`：只读工具直接执行；命中 `CONFIRM_TOOLS` 的工具被 `HumanInTheLoopMiddleware` 挂起。
5. 挂起时 `ReactAgent` 产出 `AgentEvent(type="interrupt")`，`app.py` 渲染确认卡片；用户批准后用 `Command(resume=...)` 继续。
6. 循环直到模型返回最终 `AIMessage`；`app.py` 用 `_render_events` 渲染文本并 `st.rerun()`。

## 5. 电脑操作与安全模型

**工作区域**：`config/agent.yml` 的 `workspace_roots` 决定所有路径操作的边界（默认用户目录 + 项目根）；敏感目录（`.ssh`、`.aws` 等）一律拒绝。读取、搜索、写入、删除、命令 `cwd` 都受限。

**只读工具（直接执行）**：`list_directory`、`read_text_file`、`search_files`、`search_text`，模型调用后立即执行，但仍受工作区限制。

**敏感读取（需确认）**：`get_system_info`、`list_processes`，会读取设备标识或正在使用的软件。

**修改类工具（需确认）**：`write_text_file`、`create_directory`、`move_path`、`copy_path`、`delete_path`、`run_command`、`open_path`，统一在 `CONFIRM_TOOLS` 注册，由 `HumanInTheLoopMiddleware` 拦截。确认卡片用大白话说明“做什么、目标、风险、影响范围”。

**自动保险**：删除默认移到 `.agent_trash`；覆盖前备份到 `.agent_backup`；可执行后缀拒绝写入；28 条危险命令黑名单（含环境变量、编码命令、`cd ..`、下载/执行工具等绕过路径）；命令中的越界路径拒绝；单轮最多 20 次工具调用。

**操作留痕**：聊天页「本次操作记录」展示每一步的人话解释；`logs/audit_YYYYMMDD.jsonl` 记录结构化审计日志。

**报告场景**：模型调用 `fill_context_for_report` → `monitor_tool` 设置 `runtime.context["report"]=True` → 下一次模型调用由 `report_prompt_switch` 切换到 `report_prompt.txt`。标记只在本次请求内有效。

## 6. 目录速查

```text
agent/        Agent 组装、13 个电脑工具 + 7 个业务工具、安全策略与中间件
model/        Chat 模型工厂
utils/        配置、路径、日志、提示词加载
config/       model / agent / prompts
prompts/      main / report
data/         external/records.csv
memorys/      记忆方式工厂 + 短期/JSON/PostgreSQL checkpointer
docs/         架构文档、电脑操作功能说明与 SVG
env_patch/    .pth + sitecustomize.py
```

## 7. 关键约束

- **默认短期记忆**：输入框左侧的记忆按钮可选短期（默认，不存档）/ 本地 JSON 存档 / PostgreSQL；侧边栏管理会话 ID。
- **工作区域是硬边界**：所有文件操作（包括读取）必须在 `workspace_roots` 内；敏感目录默认拒绝；可在输入框左侧「工作区域」按钮修改，保存后立即生效。
- **消息块级流式**：只输出不带 `tool_calls` 的 `AIMessage` 文本，工具中间消息不显示；操作记录单独展示。
- **高危操作必须确认**：确认卡片用大白话解释“做什么、目标、风险、影响范围”；目前只支持全部确认/全部拒绝。
- **删除默认进回收区**：`.agent_trash` 可手动恢复；`permanent=True` 才永久删除。
- **命令检查不是沙箱**：黑名单 + 路径提取是尽力而为，确认卡片是最后防线。
- **无 GUI 自动化**：只有文件系统、进程与命令行能力，不能点击按钮、切换窗口或截图。
- **外部 CSV 为空**：`data/external/records.csv` 当前 0 字节；`fetch_external_data` 注解为 `str`、实际返回 `dict`。
- **未实现**：鉴权、限流、多租户、自动化测试、文件上传 UI；PII 仅覆盖模型输入/输出，日志未脱敏。

## 8. 按问题找位置

| 我想知道 | 看这里 |
| --- | --- |
| 项目怎么跑起来？ | [README 快速开始](../README.md#4-快速开始) |
| 整体分几层、用了什么？ | [ARCHITECTURE 第 4 节](ARCHITECTURE.md#4-总体架构) + [architecture.svg](architecture.svg) |
| 一次请求怎么走？ | [ARCHITECTURE 第 6 节](ARCHITECTURE.md#6-运行时主链路) |
| 电脑操作与确认怎么实现？ | [ARCHITECTURE 第 7 节](ARCHITECTURE.md#7-电脑操作子系统) |
| 电脑操作有哪些功能、怎么确认？ | [OS_TOOLS.md](OS_TOOLS.md) |
| 报告场景怎么切换？ | [ARCHITECTURE 第 8 节](ARCHITECTURE.md#8-报告场景与动态提示词) |
| 配置在哪改？ | [ARCHITECTURE 第 9 节](ARCHITECTURE.md#9-配置体系) + [README 配置速查](../README.md#7-配置入口速查) |
| 想加工具/上传/记忆？ | [ARCHITECTURE 第 13 节](ARCHITECTURE.md#13-扩展指南) |
| 有什么坑/技术债？ | [ARCHITECTURE 第 12 节](ARCHITECTURE.md#12-已知问题与技术债) |
| 启动失败/日志在哪？ | [ARCHITECTURE 第 11 节](ARCHITECTURE.md#11-可观测性与错误处理) + [16.2](ARCHITECTURE.md#162-常用命令) |
| 代码文件对应关系？ | [ARCHITECTURE 16.1](ARCHITECTURE.md#161-关键文件索引) |
| 最近改了什么、为什么改？ | [CHANGELOG](../CHANGELOG.md) |

## 9. 给 AI 的阅读约定

完整约定见 [AGENTS.md](../AGENTS.md)。核心四点：

- 先读本文件，再按第 8 节索引只读 `ARCHITECTURE.md` 对应章节，不要整份加载。
- 不要为了建立认知去整份读 `docs/*.svg`；图是给人看的，文字模块表更适合 AI（改图时只 grep `<text>` 节点定位）。
- 代码是最终事实源；文档与代码冲突时，以代码为准并更新文档。
- 增量变更写 [CHANGELOG.md](../CHANGELOG.md)，不要塞进架构正文。
