"""工具注册表：所有注册给模型的工具集中在这里。

为什么单独一层：

- `react_agent.py` 只负责编排，不再维护一份“工具清单”副本；
- 文档一致性脚本从本模块取 `len(AGENT_TOOLS)`、`len(CONFIRM_TOOLS)`，
  避免 README/架构文档里的数字与代码漂移；
- 新增/删除工具时只改这一处，`tests/test_docs_consistency.py` 会提醒同步文档。

注意：这里只做“注册”，不改变任何工具的权限或确认策略。每个工具的
只读/副作用分级仍由 `agent/guard/policy.py` 与 `CONFIRM_TOOLS` 决定。
"""

from agent.tools.agent_tools import (
    fetch_external_data,
    fill_context_for_report,
    get_current_month,
    get_user_id,
    get_user_location,
    get_weather,
    web_search_baidu,
)
from agent.tools.computer_use import computer_use
from agent.tools.os_tools import (
    copy_path,
    create_directory,
    delete_path,
    get_system_info,
    list_directory,
    list_processes,
    move_path,
    open_path,
    read_text_file,
    run_command,
    search_files,
    search_text,
    write_text_file,
)

# 按“能力类别”分组，方便文档脚本生成 13 + 7 + 1 这类数字。
OS_TOOLS = [
    get_system_info,
    list_directory,
    read_text_file,
    search_files,
    search_text,
    list_processes,
    write_text_file,
    create_directory,
    move_path,
    copy_path,
    delete_path,
    run_command,
    open_path,
]

BUSINESS_TOOLS = [
    get_weather,
    get_user_location,
    get_user_id,
    get_current_month,
    fetch_external_data,
    fill_context_for_report,
    web_search_baidu,
]

DESKTOP_TOOLS = [computer_use]

# 真正注册给模型的顺序；保持与旧版 `react_agent.py` 一致（桌面控制在业务工具之前）。
AGENT_TOOLS = [*OS_TOOLS, *DESKTOP_TOOLS, *BUSINESS_TOOLS]

# 会读取敏感信息或修改电脑状态的工具，调用前必须由用户确认。
# 描述用于确认卡片/提示词，不参与权限判断。
CONFIRM_TOOLS = {
    "get_system_info": "查看系统信息",
    "list_processes": "查看进程列表",
    "write_text_file": "写入文件",
    "create_directory": "创建目录",
    "move_path": "移动或重命名",
    "copy_path": "复制文件",
    "delete_path": "删除文件或目录",
    "run_command": "执行系统命令",
    "open_path": "打开文件或网址",
    "computer_use": "控制桌面（截图/鼠标/键盘/剪贴板/应用）",
}

# 工具调用的硬上限，防止模型陷入死循环；每任务的实际步数预算
# 由任务卡 + 守卫层（agent/guard/state.py）控制。
TOOL_CALL_LIMIT = 100
