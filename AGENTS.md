# AGENTS.md

给编码助手/自动化 Agent 的项目阅读约定。目标：用尽量少的上下文建立正确认知，避免整份加载长文档。

## 阅读顺序

1. `docs/PROJECT_OVERVIEW.md`：先建立全局（定位、分层、主流程、关键约束）。
2. `README.md` 的「快速开始」「配置入口速查」：只在需要命令/环境变量时读。
3. 需要电脑操作的功能清单、参数与安全边界时，读 `docs/OS_TOOLS.md`。
4. 需要安全评估、威胁模型、技术债与改进优先级时，读 `docs/SECURITY_REPORT.md`。
5. 需要 Agent 安全设计的通用经验、反模式和落地路线时，读 `docs/AGENT_SECURITY_PLAYBOOK.md`。
6. 需要了解 OmniPilot 桌面控制的接入背景、用法、限制与风险时，读 `docs/OMNIPILOT_INTEGRATION_REPORT.md`（小白版报告）。
7. 要改工具失败/重试/结果校验/会话自愈，或要跑回归验证时，读 `docs/agent-resilience/00-进度备忘录.md` + `01-入门讲解-失败重试与结果验证.md` + `02-验证与交付复盘.md`；回归命令是 `python scripts/verify_agent_resilience.py`。
8. 要改任务卡/只读/批次回滚/预算急停/审计回放/单任务锁/第二主体，或要跑 P0 验收时，先读 `docs/small-business-p0/00-进度备忘录.md`，再按主题读 `01`–`08`；回归命令是 `python scripts/verify_small_business.py`。
9. `docs/ARCHITECTURE.md` 的对应章节：按 `PROJECT_OVERVIEW.md` 第 8 节索引按需读，不要默认整份加载。
10. 源码：需要精确行为时再读；代码是最终事实源。
11. `CHANGELOG.md`：只在需要知道“最近改了什么 / 为什么改”时读；它是 Keep a Changelog 格式的人工摘要，不是 git log。

## 不要做的事

- 不要为了建立认知去整份读 `docs/*.svg`。图是给人看的，文字模块表更适合 AI。但**核对/更新图里的文字时**，用 grep 只取 `<text>` 节点定位，改完用 XML 解析校验一次。
- 不要假设 `utils/runtime.py` 存在；当前运行时初始化由 `env_patch/` 的 `.pth + sitecustomize.py` 完成，它们需要执行 `uv run python scripts/apply_env_patch.py` 装进 `.venv`（`uv sync` 重建后会丢失）。
- 不要把 `ARCHITECTURE.md` 里的“示例/预留”当成已接入主链路；状态标记以 ✅ / 🧪 / ⚙️ / ⚠️ 为准。
- 不要把 `.agent_state/` 当源码或文档提交：它是运行时状态（批次/会话快照/锁/STOP），已在 `.gitignore`；测试要用 `AGENT_STATE_DIR` / `AGENT_APPROVAL_DIR` 指到临时目录，避免污染项目状态。
- 不要把审批口令（PIN）写进 `.env`、提示词、文档、聊天记录或 Git；只能用 `scripts/setup_approval_pin.py` 写入本机审批目录（只存哈希）。

## 修改后的文档动作

按 `docs/ARCHITECTURE.md` 第 16.3 节同步：

- 新增/删除工具 → `README.md` 能力表、`ARCHITECTURE.md` 5.2/13.1、`docs/OS_TOOLS.md`、`architecture.svg` 工具层。
- 新增/删除中间件 → `ARCHITECTURE.md` 5.2/8/11、`architecture.svg` 编排层。
- 新增/删除文档 → `README.md` 导读/文档地图、`PROJECT_OVERVIEW.md` 第 8 节、本文件阅读顺序。
- 新增/修改测试用例或验证脚本 → `tests/`、`scripts/verify_agent_resilience.py`、`scripts/verify_small_business.py`、`docs/agent-resilience/02-验证与交付复盘.md`（用例数与结论要同步）、`docs/small-business-p0/08-交付复盘与验收清单.md`（P0 用例数与证据）、`ARCHITECTURE.md` 13.10、`README.md` 能力表。
- 改工具失败/重试/结果校验/会话自愈 → `agent/resilience.py`、`agent/tools/verification.py`、`agent/tools/middleware.py` 的 `RETRYABLE_TOOLS` / `_should_retry_tool_error`、`ARCHITECTURE.md` 6.5/11.3/12、`docs/agent-resilience/01`，并补对应用例。
- 改电脑操作/人工确认 → `ARCHITECTURE.md` 7/13.2、`docs/OS_TOOLS.md`、`README.md` 能力表、`architecture.svg` 工具层。
- 改安全策略/工作区域 → `ARCHITECTURE.md` 7.2/7.4、`docs/OS_TOOLS.md` 2/8/11.2/11.3、`README.md` 配置速查；界面入口在 `app.py` 的「工作区域」弹层。
- 改守卫层（任务卡/只读/批次/预算/急停/锁/PIN）→ `agent/guard/`、`tests/test_small_business_guard.py`、`scripts/verify_small_business.py`、`ARCHITECTURE.md` 4.1/5.2/6/7/8/9/11/12/13.10/14/16.3、`docs/small-business-p0/00–08`、`README.md`、`PROJECT_OVERVIEW.md`、`architecture.svg` 守卫层。
- 改配置 → `ARCHITECTURE.md` 9、`README.md` 配置速查。
- 改模型/依赖/目录 → 对应技术栈、拓扑与目录树章节；**图也要同步**：`architecture.svg` 的模型/存储层节点与副标题。
- 任何值得记录的改动（新增/变更/废弃/移除/修复）→ 归入 `CHANGELOG.md` 的 `## [Unreleased]`，按 Keep a Changelog 分组（Added / Changed / Deprecated / Removed / Fixed / Security）写英文标题 + 中文描述；发布时才改成版本标题。不要把增量日志写进架构正文。

## 冲突处理

文档与代码冲突时，以代码为准；修正文档，并在 `docs/ARCHITECTURE.md` 第 12 节更新技术债状态。历史变更交给 Git / `CHANGELOG.md`，不要把增量日志写进架构正文。