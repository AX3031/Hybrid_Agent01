import os
from dataclasses import dataclass, field
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware, ToolCallLimitMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from model.factory import chat_model
from utils.prompt_loader import load_system_prompts
from memorys.memory_factory import DEFAULT_MEMORY_MODE, create_checkpointer
from agent.tools.agent_tools import (
    get_weather, get_user_location, get_user_id, get_current_month,
    fetch_external_data, fill_context_for_report, web_search_baidu,
)
from agent.tools.computer_use import action_requires_confirmation, computer_use
from agent.tools.safety import summarize_content_for_log
from agent.tools.os_tools import (
    get_system_info, list_directory, read_text_file, search_files, search_text, list_processes,
    write_text_file, create_directory, move_path, copy_path, delete_path, run_command, open_path,
)
from agent.tools.middleware import (
    monitor_tool, log_before_model, report_prompt_switch,
    pii_middleware_email, pii_middleware_phone, model_fallback_middleware,
)

# 会读取敏感信息或修改电脑状态的工具，调用前必须由用户确认
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

# 人工确认中间件：命中 CONFIRM_TOOLS 的工具调用会挂起，等待 UI 提交 approve/reject
_HITL_CONFIG: dict[str, Any] = {
    name: {"allowed_decisions": ["approve", "reject"]}
    for name in CONFIRM_TOOLS
}
# computer_use 是一个多动作工具：只读动作（屏幕尺寸/找图/等待）直接放行，
# 截图、剪贴板、应用列表和所有鼠标键盘动作仍然弹确认。
_HITL_CONFIG["computer_use"]["when"] = lambda request: action_requires_confirmation(
    (request.tool_call.get("args") or {}).get("action")
)

hitl_middleware = HumanInTheLoopMiddleware(
    interrupt_on=_HITL_CONFIG,
    description_prefix="该操作会修改电脑状态，请确认参数后再决定是否执行",
)

# 单轮对话最多 20 次工具调用，防止模型陷入循环或误操作
TOOL_CALL_LIMIT = 20
tool_limit_middleware = ToolCallLimitMiddleware(
    run_limit=TOOL_CALL_LIMIT,
    exit_behavior="continue",
)


@dataclass
class AgentEvent:
    """Agent 流式事件。

    - type="text"：模型输出的一段文本，content 为内容
    - type="interrupt"：有危险操作等待确认，action_requests 为待确认的工具调用
    - type="tool"：一个工具调用已返回结果，tool_call 包含名称、参数、结果、状态
    """

    type: str
    content: str = ""
    action_requests: list[dict[str, Any]] = field(default_factory=list)
    tool_call: dict[str, Any] | None = None


def _default_memory_mode() -> str:
    """兼容旧配置：未显式指定记忆方式时，环境变量仍可切到 PostgreSQL。"""
    if os.getenv("USE_POSTGRES_CHECKPOINTER", "").lower() in {"1", "true", "yes", "on"}:
        return "postgres"
    return DEFAULT_MEMORY_MODE


class ReactAgent:
    def __init__(self, memory_mode: str | None = None):
        # 记忆方式：short_term（默认）/ file / postgres
        self.memory_mode = memory_mode or _default_memory_mode()
        self.checkpointer = create_checkpointer(self.memory_mode)
        # 确认后恢复执行时，用于把 ToolMessage 与之前被挂起的操作配对
        self._resume_pending_actions: list[dict] = []
        self.agent = create_agent(
            model=chat_model,
            system_prompt=load_system_prompts(),
            tools=[
                # 电脑信息（只读）
                get_system_info, list_directory, read_text_file,
                search_files, search_text, list_processes,
                # 电脑操作（需用户确认）
                write_text_file, create_directory, move_path, copy_path,
                delete_path, run_command, open_path,
                # 桌面控制（OmniPilot 本地工具层；只读动作直接执行，其余需确认）
                computer_use,
                # 原有业务工具
                get_weather, get_user_location, get_user_id, get_current_month,
                fetch_external_data, fill_context_for_report, web_search_baidu,
            ],
            middleware=[
                model_fallback_middleware,
                pii_middleware_email,
                pii_middleware_phone,
                monitor_tool,
                log_before_model,
                report_prompt_switch,
                hitl_middleware,
                tool_limit_middleware,
            ],
            checkpointer=self.checkpointer,
        )

    def execute_stream(self, query: str, thread_id: str | None = None):
        """提交一条用户消息，流式返回文本、工具记录与待确认事件。"""
        self._resume_pending_actions = []
        input_dict = {
            "messages": [
                {"role": "user", "content": query}
            ]
        }
        yield from self._stream(input_dict, self._build_config(thread_id))

    def resume_stream(
        self,
        decisions: list[dict],
        thread_id: str | None = None,
        pending_actions: list[dict] | None = None,
    ):
        """用户确认后继续执行被挂起的工具调用。

        decisions 需与待确认操作一一对应，例如：
        [{"type": "approve"}] 或 [{"type": "reject", "message": "用户拒绝执行"}]
        pending_actions 传入待确认的 action_requests，用于把执行结果补进操作记录。
        """
        self._resume_pending_actions = list(pending_actions or [])
        yield from self._stream(Command(resume={"decisions": decisions}), self._build_config(thread_id))

    @staticmethod
    def _build_config(thread_id: str | None) -> dict:
        # checkpointer 始终存在，thread_id 用于区分会话（相同 ID 恢复历史）
        return {"configurable": {"thread_id": thread_id or "default"}}

    def _stream(self, payload: Any, config: dict):
        pending_calls: dict[str, dict] = {}

        try:
            # 同时订阅 values（完整状态，用于取模型回答）和 updates（用于捕获 interrupt 与工具结果）
            for mode, chunk in self.agent.stream(
                payload,
                stream_mode=["values", "updates"],
                context={"report": False},
                config=config,
            ):
                if mode == "updates":
                    if "__interrupt__" in chunk:
                        action_requests = []
                        for interrupt_obj in chunk["__interrupt__"]:
                            value = getattr(interrupt_obj, "value", {}) or {}
                            action_requests.extend(value.get("action_requests", []))
                        if action_requests:
                            yield AgentEvent(type="interrupt", action_requests=action_requests)
                        continue

                    for node_update in chunk.values():
                        if not isinstance(node_update, dict):
                            continue
                        for message in node_update.get("messages", []) or []:
                            if isinstance(message, AIMessage) and message.tool_calls:
                                for tool_call in message.tool_calls:
                                    pending_calls[tool_call["id"]] = tool_call
                            elif isinstance(message, ToolMessage):
                                tool_call = pending_calls.pop(message.tool_call_id, None)
                                if tool_call is None and self._resume_pending_actions:
                                    # 恢复执行时可能还有只读工具先返回，按工具名匹配，避免错配
                                    message_name = getattr(message, "name", "") or ""
                                    for index, action in enumerate(self._resume_pending_actions):
                                        if action.get("name") == message_name:
                                            action = self._resume_pending_actions.pop(index)
                                            tool_call = {
                                                "name": action.get("name", ""),
                                                "args": action.get("args", {}),
                                            }
                                            break
                                if tool_call:
                                    content = summarize_content_for_log(message.content)
                                    yield AgentEvent(
                                        type="tool",
                                        tool_call={
                                            "name": tool_call.get("name", ""),
                                            "args": tool_call.get("args", {}),
                                            "result": content,
                                            "status": getattr(message, "status", None) or "success",
                                        },
                                    )
                    continue

                messages = chunk.get("messages") or []
                if not messages:
                    continue
                lastest_message = messages[-1]
                # 只输出模型回答，避免把用户问题、工具结果回显到页面
                if isinstance(lastest_message, AIMessage) and not lastest_message.tool_calls:
                    if isinstance(lastest_message.content, str) and lastest_message.content.strip():
                        yield AgentEvent(type="text", content=lastest_message.content.strip() + "\n")
        finally:
            self._resume_pending_actions = []

    def clear_memory(self, thread_id: str) -> None:
        """删除指定会话在后端 checkpointer 中的全部状态。"""
        if thread_id and self.checkpointer is not None:
            self.checkpointer.delete_thread(thread_id)

    def load_ui_messages(self, thread_id: str) -> list[dict]:
        """读取指定会话的历史消息，供 UI 在刷新/切换后恢复聊天记录。"""
        if not thread_id or self.checkpointer is None:
            return []

        checkpoint_tuple = self.checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})
        if checkpoint_tuple is None:
            return []

        messages = checkpoint_tuple.checkpoint.get("channel_values", {}).get("messages", [])
        ui_messages = []
        for message in messages:
            if isinstance(message, HumanMessage):
                role = "user"
            elif isinstance(message, AIMessage) and not message.tool_calls:
                role = "assistant"
            else:
                continue
            if isinstance(message.content, str) and message.content.strip():
                ui_messages.append({"role": role, "content": message.content.strip()})
        return ui_messages


if __name__ == '__main__':
    agent = ReactAgent()

    for event in agent.execute_stream("看一下这台电脑的系统信息", thread_id="local_test"):
        if event.type == "text":
            print(event.content, end="", flush=True)
        elif event.type == "tool":
            print(f"\n[操作] {event.tool_call}")
        else:
            print(f"\n[待确认操作] {event.action_requests}")
