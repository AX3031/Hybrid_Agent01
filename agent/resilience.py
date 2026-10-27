"""会话中断后的自愈：补齐“没有结果的工具调用”。

背景（为什么需要这个模块）：

- LangGraph 的 `ToolNode` 默认不兜底异常。`_default_handle_tool_errors` 只把
  `ToolInvocationError` 转成错误消息，其它异常一律重新抛出；
- 工具一旦抛异常，整条 run 会中断，checkpoint 里留下一条带 `tool_calls` 的
  `AIMessage`，却没有对应的 `ToolMessage`，图的 `next` 还停在 `tools` 节点；
- 下一次在同一个 thread 里提问时，这份“tool_calls 没有结果”的历史会被发给
  模型 API，常见后果是 400 报错或模型被带偏。

所以每次提交新消息之前，先调用 `repair_thread_state()` 把这类调用补成
`status="error"` 的占位结果。补的是“没执行过”的事实，不是伪造成功。
"""

from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from utils.logger_handler import logger

# create_agent 内部 ToolNode 的默认节点名
TOOL_NODE_NAME = "tools"

_INTERRUPTED_PLACEHOLDER = (
    "[未执行] 上一次运行在调用 `{name}` 时中断（进程异常、超时或用户停止），"
    "这次调用没有产生结果。不要假设它已经成功；如果任务仍需完成，请重新发起该操作。"
)


def find_unanswered_tool_calls(messages: list[Any]) -> list[dict]:
    """找出所有“有调用、没结果”的 tool_call，按出现顺序返回。

    判断依据是 `tool_call_id`：只要有任意一条 `ToolMessage` 回填了该 id，
    就视为已产生结果（成功或失败都算）。
    """
    answered = {
        message.tool_call_id
        for message in messages
        if isinstance(message, ToolMessage) and message.tool_call_id
    }

    pending: list[dict] = []
    seen: set[str] = set()
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls or []:
            call_id = call.get("id")
            if not call_id or call_id in answered or call_id in seen:
                continue
            seen.add(call_id)
            pending.append(call)
    return pending


def build_interrupted_tool_messages(calls: list[dict]) -> list[ToolMessage]:
    """为中断的调用生成占位 ToolMessage（状态固定为 error）。"""
    messages: list[ToolMessage] = []
    for call in calls:
        name = call.get("name") or "未知工具"
        messages.append(
            ToolMessage(
                content=_INTERRUPTED_PLACEHOLDER.format(name=name),
                tool_call_id=call.get("id") or "",
                name=name,
                status="error",
            )
        )
    return messages


def repair_thread_state(graph: Any, config: dict, *, node_name: str = TOOL_NODE_NAME) -> list[dict]:
    """把指定 thread 的悬空工具调用补齐，返回被修复的调用列表。

    以 `as_node="tools"` 写回，相当于“工具节点补交了结果”，这样图的 `next`
    会回到 `model`，thread 可以继续对话。修复失败不抛异常，只记 warning：
    自愈是尽力而为，不能因为自愈失败把用户的新请求也挡掉。
    """
    try:
        state = graph.get_state(config)
    except Exception as exc:  # noqa: BLE001 - 自愈失败不应影响主流程
        logger.warning(f"[resilience]读取会话状态失败，跳过自愈：{exc}")
        return []

    pending = find_unanswered_tool_calls((state.values or {}).get("messages", []))
    if not pending:
        return []

    try:
        graph.update_state(
            config,
            {"messages": build_interrupted_tool_messages(pending)},
            as_node=node_name,
        )
    except Exception as exc:  # noqa: BLE001 - 同上
        logger.warning(f"[resilience]补齐中断的工具调用失败：{exc}")
        return []

    names = "、".join(str(call.get("name") or "?") for call in pending)
    logger.warning(f"[resilience]已为上次中断遗留的 {len(pending)} 个工具调用补占位结果：{names}")
    return pending
