# AGENTS.md

给编码助手/自动化 Agent 的项目阅读约定。目标：用尽量少的上下文建立正确认知，避免整份加载长文档。

## 阅读顺序

1. `docs/PROJECT_OVERVIEW.md`：先建立全局（定位、分层、主流程、关键约束）。
2. `README.md` 的「快速开始」「配置入口速查」：只在需要命令/环境变量时读。
3. `docs/ARCHITECTURE.md` 的对应章节：按 `PROJECT_OVERVIEW.md` 第 8 节索引按需读，不要默认整份加载。
4. 源码：需要精确行为时再读；代码是最终事实源。
5. `CHANGELOG.md`：只在需要知道“最近改了什么 / 为什么改”时读；它是 Keep a Changelog 格式的人工摘要，不是 git log。

## 不要做的事

- 不要为了建立认知去整份读 `docs/*.svg`。图是给人看的，文字模块表更适合 AI。但**核对/更新图里的文字时**，用 grep 只取 `<text>` 节点定位，改完用 XML 解析校验一次。
- 不要假设 `utils/runtime.py` 存在；当前运行时初始化由 `env_patch/` 的 `.pth + sitecustomize.py` 完成。
- 不要把 `ARCHITECTURE.md` 里的“示例/预留”当成已接入主链路；状态标记以 ✅ / 🧪 / ⚙️ / ⚠️ 为准。

## 修改后的文档动作

按 `docs/ARCHITECTURE.md` 第 16.3 节同步：

- 新增/删除工具 → `README.md` 能力表、`ARCHITECTURE.md` 5.2/13.1、`architecture.svg` 工具层。
- 新增/删除中间件 → `ARCHITECTURE.md` 5.2/8/11、`architecture.svg` 编排层。
- 改电脑操作/人工确认 → `ARCHITECTURE.md` 7/13.2、`README.md` 能力表、`architecture.svg` 工具层。
- 改配置 → `ARCHITECTURE.md` 9、`README.md` 配置速查。
- 改模型/依赖/目录 → 对应技术栈、拓扑与目录树章节；**图也要同步**：`architecture.svg` 的模型/存储层节点与副标题。
- 任何值得记录的改动（新增/变更/废弃/移除/修复）→ 归入 `CHANGELOG.md` 的 `## [Unreleased]`，按 Keep a Changelog 分组（Added / Changed / Deprecated / Removed / Fixed / Security）写英文标题 + 中文描述；发布时才改成版本标题。不要把增量日志写进架构正文。

## 冲突处理

文档与代码冲突时，以代码为准；修正文档，并在 `docs/ARCHITECTURE.md` 第 12 节更新技术债状态。历史变更交给 Git / `CHANGELOG.md`，不要把增量日志写进架构正文。