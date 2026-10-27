"""守卫层中间件：把只读、任务卡、预算、急停接进 ReAct 循环。

顺序（在 `ReactAgent` 的 middleware 列表里）：

    monitor_tool → guard_preflight → HITL → guard_execute → 工具

- `guard_preflight` 在人工确认**之前**检查，避免让用户批准一个本来就
  不该执行的动作；
- `guard_execute` 在人工确认**之后**再检查一次并记步数，保证“批准的
  那一次”才消耗预算；
- `guard_model` 在每次模型调用前短路：预算/急停触发后不再给模型新的
  决策机会；
- `track_model_usage` 在模型返回后累计 token 与费用（有 usage 才计费）。
"""

from langchain.agents.middleware import (
    ModelRequest,
    ModelResponse,
    after_model,
    wrap_model_call,
    wrap_tool_call,
)
from langchain_core.messages import AIMessage, ToolMessage

from agent.guard import approval
from agent.guard import policy
from agent.guard import repeat_guard
from agent.guard import state as guard_state
from utils.logger_handler import logger


def _error_message(request, tool_name: str, reason: str) -> ToolMessage:
    return ToolMessage(
        content=(
            f"[守卫层拒绝] {reason}\n"
            "这次调用没有执行。不要尝试绕过；如果任务确实需要，请让用户调整任务卡、只读开关或预算。"
        ),
        tool_call_id=str((request.tool_call or {}).get("id") or ""),
        name=tool_name,
        status="error",
    )


@wrap_tool_call
def guard_preflight(request, handler):
    """人工确认前的确定性检查。"""
    tool_name = str((request.tool_call or {}).get("name") or "")
    args = (request.tool_call or {}).get("args") or {}
    reason = policy.preflight_reason(tool_name, args)
    if reason:
        logger.warning(f"[guard]预检拒绝 {tool_name}：{reason}")
        return _error_message(request, tool_name, reason)
    return handler(request)


def _append_note(result, note: str):
    """把“换策略”提醒附在工具结果后面；不改变结果本身的成功/失败状态。"""
    if not note or not isinstance(result, ToolMessage):
        return result
    content = result.content
    if isinstance(content, str):
        return result.model_copy(update={"content": f"{content}\n\n{note}"})
    if isinstance(content, list):
        return result.model_copy(update={"content": [*content, {"type": "text", "text": note}]})
    return result


@wrap_tool_call
def guard_execute(request, handler):
    """人工确认后的最后一道检查，并记录一步预算。"""
    tool_name = str((request.tool_call or {}).get("name") or "")
    args = (request.tool_call or {}).get("args") or {}
    reason = guard_state.task_block_reason()
    if not reason:
        reason = policy.check_read_only(tool_name, args)
    if not reason and policy.requires_second_subject(tool_name, args):
        context = guard_state.approval_context(tool_name)
        token = str(context.get("token") or "")
        if not token:
            reason = (
                "该动作属于 R2/R3，需要人工审批口令（PIN）后才能执行；"
                "当前没有有效的审批凭证。"
            )
        else:
            task = guard_state.current()
            thread_id = str(context.get("thread_id") or (task.thread_id if task else ""))
            batch_id = str(context.get("batch_id") or (task.batch_id if task else ""))
            approved_actions = context.get("actions") or []
            if not thread_id or not batch_id:
                reason = (
                    "审批凭证缺少任务/批次绑定，无法证明它属于当前任务；"
                    "这次 R2/R3 动作没有执行。"
                )
            elif not approval.verify_approval_token(
                token,
                thread_id=thread_id,
                batch_id=batch_id,
                actions=approved_actions,
            ):
                reason = (
                    "审批凭证无效、已过期，或与当前任务/批次/动作不匹配；"
                    "这次 R2/R3 动作没有执行。请重新发起审批。"
                )
    if reason:
        logger.warning(f"[guard]执行前拒绝 {tool_name}：{reason}")
        return _error_message(request, tool_name, reason)

    # 原地打转检测：同一处反复做同一动作时先提醒、再拦截，避免继续烧预算。
    task = guard_state.current()
    repeats, advice, loop_reason = repeat_guard.note_action(
        task.batch_id if task else "", tool_name, args
    )
    if loop_reason:
        logger.warning(f"[guard]重复动作拦截 {tool_name}（第 {repeats} 次）")
        return _error_message(request, tool_name, loop_reason)

    guard_state.consume_step()
    return _append_note(handler(request), advice)


@wrap_model_call
def guard_model(request: ModelRequest, handler):
    """预算/急停触发后，不再让模型继续规划新动作。"""
    reason = guard_state.task_block_reason()
    if reason:
        guard_state.mark_stopped(reason)
        logger.warning(f"[guard]停止本轮任务：{reason}")
        return ModelResponse(
            result=[
                AIMessage(
                    content=(
                        f"任务已停止：{reason}。\n"
                        "已经把现场保留下来；你可以先在界面查看批次记录，必要时整批回滚。"
                        "如果没有把握，请把控制权交给人。"
                    )
                )
            ]
        )
    return handler(request)


@after_model
def track_model_usage(state, runtime):
    """累计模型调用与 token 用量。"""
    messages = state.get("messages") or []
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            usage = getattr(message, "usage_metadata", None) or {}
            guard_state.add_usage(
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0),
            )
            break
    return None
