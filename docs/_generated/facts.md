<!-- 由 scripts/sync_doc_facts.py 生成；不要手改。 -->
# 当前代码事实（自动生成）

> 本页是工具、动作、用例、预算、模型等“数字”的唯一事实源；README/架构文档只保留“为什么”。
> 重新生成：`uv run python scripts/sync_doc_facts.py`；只校验：`uv run python scripts/sync_doc_facts.py --check`。
> 安全报告、阶段复盘、CHANGELOG 等历史文档里的数字是当时快照，不随本页变化。

## 1. 工具与动作

| 项 | 当前值 |
| --- | --- |
| 注册工具 | 21 个（电脑操作 13 + 业务 7 + 桌面控制 1） |
| 需要确认的工具 | 10 个 |
| `computer_use` 动作 | 26 个（直接放行 4 个，需确认 22 个，控件级/OCR 7 个） |

## 2. 规则与预算

| 项 | 当前值 |
| --- | --- |
| 危险命令规则 | 29 条 |
| 可执行写入后缀 | 17 种 |
| 桌面应用黑名单 | 42 个条目 |
| 系统关键进程 | 17 个 |
| 普通任务预算 | 30 步 / 10 分钟 / 2 元 |
| 桌面任务预算 | 60 步 / 20 分钟 / 5 元 |
| 工具调用硬上限 | 100 步/单次运行 |
| 原地打转阈值 | 第 3 次提醒 / 第 6 次拒绝 |
| 发给模型的最近截图 | 1 张 |

## 3. 默认配置与模型

| 项 | 当前值 |
| --- | --- |
| 主模型 / 备用模型 | `qwen3.5-27b` / `qwen3.5-flash` |
| 默认工作区 | 由 `config/agent.yml` 的 `workspace_roots` 决定（本页不落具体路径） |
| 默认只读 | 由 `config/agent.yml` 的 `read_only` 决定（本页不落具体值） |
| 机器判据类型 | absent、exists、file_count、file_size、sha256、text_contains、text_not_contains |
| 任务卡动作类别 | `读`、`写`、`移动`、`复制`、`删除`、`命令`、`打开`、`桌面`、`联网` |
| 默认禁止动作 | 发送、提交、付款、提权 |
| 快速卡默认禁止 | 提权 |

## 4. 离线用例

| 项 | 当前值 |
| --- | --- |
| 全部用例 | 202 条 |
| P0 守卫层 | 62 条 |

## 5. 注册工具清单

`get_system_info`、`list_directory`、`read_text_file`、`search_files`、`search_text`、`list_processes`、`write_text_file`、`create_directory`、`move_path`、`copy_path`、`delete_path`、`run_command`、`open_path`、`computer_use`、`get_weather`、`get_user_location`、`get_user_id`、`get_current_month`、`fetch_external_data`、`fill_context_for_report`、`web_search_baidu`

### 需要确认的工具

`computer_use`、`copy_path`、`create_directory`、`delete_path`、`get_system_info`、`list_processes`、`move_path`、`open_path`、`run_command`、`write_text_file`

## 6. 桌面动作清单

`screenshot`、`screen_size`、`locate_image`、`wait`、`click`、`double_click`、`right_click`、`move_mouse`、`drag`、`scroll`、`type_text`、`press_key`、`hotkey`、`clipboard_get`、`clipboard_set`、`app_open`、`app_open_start_menu`、`app_close`、`app_list`、`ui_windows`、`ui_tree`、`ui_wait`、`ui_click`、`ui_set_text`、`ocr_text`、`click_text`

### 直接放行

`locate_image`、`screen_size`、`ui_wait`、`wait`

### 控件级/OCR

`click_text`、`ocr_text`、`ui_click`、`ui_set_text`、`ui_tree`、`ui_wait`、`ui_windows`
