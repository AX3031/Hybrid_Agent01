from typing import Callable

import requests
from utils.prompt_loader import load_system_prompts,load_report_prompts
from langchain.agents import AgentState
from langchain.agents.middleware import (
    wrap_model_call,
    wrap_tool_call,
    before_model,
    dynamic_prompt,
    ModelRequest,
    ToolErrorMiddleware,
    ToolRetryMiddleware,
)
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from utils.logger_handler import logger
from utils.audit_logger import log_tool_call
from agent.tools.safety import (
    sanitize_tool_args,
    sanitize_tool_result,
    summarize_content_for_log,
)
from agent.tools.verification import enrich_tool_message
from agent.guard import policy
from agent.guard import state as guard_state
from agent.guard.config import keep_recent_screenshots
from langgraph.runtime import Runtime



@wrap_tool_call
def monitor_tool(
        # 请求的数据封装
        request: ToolCallRequest,
        # 执行的函数本身
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
)-> ToolMessage | Command:          # 工具执行的监控
    tool_name = request.tool_call["name"]
    tool_args = request.tool_call["args"]
    session_id = ""

    try:
        config = getattr(request.runtime, "config", None) or {}
        session_id = config.get("configurable", {}).get("thread_id", "")
    except Exception:
        session_id = ""

    logger.info(f"[tool monitor]工具{tool_name}开始执行")
    safe_args = sanitize_tool_args(tool_name, tool_args)
    logger.info(f"[tool monitor]工具{tool_name}传入参数:{safe_args}")

    task = guard_state.current()
    batch_id = task.batch_id if task else ""
    task_id = task.task_id if task else ""
    step_no = task.steps if task else 0
    reversibility = policy.reversibility_of(tool_name, tool_args)
    approval = guard_state.approval_context(tool_name)

    try:
        result = handler(request)
        logger.info(f"[tool monitor]工具{tool_name}调用成功")

        if tool_name == 'fill_context_for_report':
            request.runtime.context["report"] = True

        # 人工确认会让工具调用挂起：这一步不是“执行结果”，单独记一条 interrupt，
        # 避免把等待确认误记成成功，也避免把 Command 对象当成工具结果。
        if isinstance(result, Command):
            log_tool_call(
                session_id,
                tool_name,
                safe_args,
                "等待人工确认",
                "interrupt",
                batch_id=batch_id,
                task_id=task_id,
                step_no=step_no,
                reversibility=reversibility,
            )
            return result

        # 结果校验：按参数核对“声称成功”的后置条件，通不过就把状态改成 error
        verified = enrich_tool_message(tool_name, tool_args, result)
        if verified is not result and getattr(verified, "status", "success") == "error":
            logger.warning(f"[tool monitor]工具{tool_name}结果校验未通过")
        result = verified

        content = getattr(result, "content", result)
        status = getattr(result, "status", "success") or "success"
        guard_state.record_tool_status(str(status))
        log_tool_call(
            session_id,
            tool_name,
            safe_args,
            sanitize_tool_result(tool_name, tool_args, content),
            str(status),
            batch_id=batch_id,
            task_id=task_id,
            step_no=step_no,
            reversibility=reversibility,
            approver=approval.get("approver", "agent"),
            approval_token=approval.get("token", ""),
        )
        return result
    except Exception as e:
        guard_state.record_tool_status("error")
        log_tool_call(
            session_id,
            tool_name,
            safe_args,
            str(e),
            "error",
            batch_id=batch_id,
            task_id=task_id,
            step_no=step_no,
            reversibility=reversibility,
            approver=approval.get("approver", "agent"),
            approval_token=approval.get("token", ""),
        )
        logger.error(f"工具{tool_name}调用失败，原因：{str(e)}")
        raise e


@before_model
def log_before_model(
        state: AgentState,          # 整个Agent智能体中的状态记录
        runtime: Runtime,           # 记录了整个执行过程中的上下文信息
):          # 在模型执行前输出日志
    logger.info(f"[log_before_model]即将调用模型，带有{len(state['messages'])}条消息。")

    last_message = state["messages"][-1]
    # 工具结果可能是字符串，也可能是带图片的 content block 列表；统一脱敏后再记录
    logger.debug(
        f"[log_before_model]{type(last_message).__name__} | "
        f"{summarize_content_for_log(getattr(last_message, 'content', ''), limit=300)}"
    )

    return None


@dynamic_prompt         # 每一次在生成提示词之前，调用此函数
def report_prompt_switch(request: ModelRequest):            # 动态切换提示词
    is_report = request.runtime.context.get("report",False)
    if is_report:           # 是报告生成场景，返回报告生成提示词内容
        prompt = load_report_prompts()
    else:
        prompt = load_system_prompts()

    # 任务卡启用时，把它追加在系统提示词末尾；卡里只有范围与判据，没有 PIN。
    card = guard_state.current_card()
    if card:
        prompt = f"{prompt}\n\n{card.to_prompt()}"
    return prompt


from langchain.agents.middleware import PIIMiddleware, ModelFallbackMiddleware

# 敏感信息脱敏：邮箱在输出侧 redact，手机号在输入侧 mask
pii_middleware_email = PIIMiddleware(
    "email",
    strategy="redact",
    apply_to_input=False,
    apply_to_output=True,
)

pii_middleware_phone = PIIMiddleware(
    "phone_number",
    # 使用自定义正则表达式，覆盖常见的手机号/座机写法
    detector=r"(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?)?\d{3,4}[\s.-]?\d{4}",
    strategy="mask",
    apply_to_input=True,
)

# 主模型调用失败时自动切换到备用模型
from model.factory import chat_model, ChatModelFactory
from utils.config_handler import model_conf

model_fallback_middleware = ModelFallbackMiddleware(
    chat_model,
    ChatModelFactory().generator(model_name=model_conf["backup_chat_model_name"]),
)


# ---------------------------------------------------------------------------
# 工具失败策略：异常转消息 + 有限重试
#
# 顺序很重要（列表里越靠前 = 越外层）：
#   tool_error_middleware -> tool_retry_middleware -> monitor_tool -> 工具
# 异常先由重试层判断能不能重试，重试耗尽后回到错误层转成 ToolMessage，
# 这样整条 ReAct 循环不会因为一次工具崩溃而中断。
# ---------------------------------------------------------------------------

# 只对幂等/只读工具自动重试；写盘、删除、移动复制、命令、键鼠操作一律不重试。
RETRYABLE_TOOLS = [
    "web_search_baidu",
    "fetch_external_data",
    "get_system_info",
    "list_processes",
    "list_directory",
    "read_text_file",
    "search_files",
    "search_text",
]


def _should_retry_tool_error(exc: Exception) -> bool:
    """只重试“可能自己好起来”的错误。

    超时/连接中断/429/5xx 值得再试一次；401/403/404、参数错误、
    文件不存在这类确定性失败重试只会更慢、更贵，甚至放大副作用。
    """
    if isinstance(exc, requests.exceptions.HTTPError):
        response = exc.response
        if response is None:
            return False
        return response.status_code == 429 or response.status_code >= 500
    if isinstance(
        exc,
        (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            TimeoutError,
            ConnectionError,
        ),
    ):
        return True
    return False


def _tool_error_content(exc: Exception, request: ToolCallRequest) -> str:
    """把异常整理成模型能读懂、且不泄露堆栈的错误消息。"""
    tool_name = request.tool_call.get("name", "未知工具")
    detail = " ".join(str(exc).split())[:300] or "无更多信息"
    logger.error(f"[tool error]工具{tool_name}异常已转为错误消息：{type(exc).__name__}: {detail}")
    return (
        f"工具 `{tool_name}` 执行失败（{type(exc).__name__}）：{detail}\n"
        "本轮对话没有中断。请判断原因：参数问题就换参数；如果是瞬时故障，可以重新发起一次调用"
        "（系统不会自动重试所有工具，所以不要在回答里声称“已重试”）；"
        "仍然失败就向用户说明，不要假装成功。"
    )


tool_error_middleware = ToolErrorMiddleware(on_error=_tool_error_content)

tool_retry_middleware = ToolRetryMiddleware(
    max_retries=2,
    tools=RETRYABLE_TOOLS,
    retry_on=_should_retry_tool_error,
    # 重试耗尽后把异常抛给外层的错误中间件，而不是伪装成正常结果
    on_failure="error",
    initial_delay=0.3,
    backoff_factor=2.0,
    max_delay=2.0,
)


# ---------------------------------------------------------------------------
# 截图历史裁剪：让“一连串桌面动作”跑得起
#
# 只改写本次发给模型的请求，不改 checkpoint：审计与界面仍能看到每一张原图，
# 但模型只拿最近 N 张截图（旧的换成文字占位，需要旧现场时让它重新 screenshot）。
# ---------------------------------------------------------------------------

_IMAGE_BLOCK_TYPES = {"image", "image_url"}

_SCREENSHOT_DROPPED_NOTE = (
    "[历史截图已省略：系统只把最近几张图发给模型。"
    "如需确认现场，请重新调用 computer_use(action=screenshot)，不要凭旧图猜坐标。]"
)


def _is_image_block(block) -> bool:
    if isinstance(block, dict):
        return str(block.get("type", "")) in _IMAGE_BLOCK_TYPES
    return str(getattr(block, "type", "")) in _IMAGE_BLOCK_TYPES


def trim_messages_screenshots(messages: list, keep: int) -> tuple[list, int]:
    """只保留最近 `keep` 个图片块，更早的换成文字占位；返回（新消息列表, 被裁剪数）。

    同一消息里的文字部分（结果摘要、截图路径）原样保留，只替掉图片块，
    所以模型仍能看到“那一步做了什么”，只是拿不到旧像素。
    """
    if keep < 0:
        return messages, 0
    positions: list[tuple[int, int]] = []  # (消息下标, 块下标)
    for index, message in enumerate(messages):
        content = getattr(message, "content", None)
        if isinstance(content, list):
            positions.extend(
                (index, block_index)
                for block_index, block in enumerate(content)
                if _is_image_block(block)
            )
    if len(positions) <= keep:
        return messages, 0
    drop = positions[: len(positions) - keep]
    by_message: dict[int, set[int]] = {}
    for message_index, block_index in drop:
        by_message.setdefault(message_index, set()).add(block_index)

    rebuilt: list = []
    for index, message in enumerate(messages):
        dropped_blocks = by_message.get(index)
        if not dropped_blocks:
            rebuilt.append(message)
            continue
        content = list(message.content)
        for block_index in dropped_blocks:
            content[block_index] = {"type": "text", "text": _SCREENSHOT_DROPPED_NOTE}
        rebuilt.append(message.model_copy(update={"content": content}))
    return rebuilt, len(drop)


@wrap_model_call
def trim_screenshot_history(request: ModelRequest, handler):
    """在真正调用模型前裁掉历史里的旧截图，避免每张图反复重发、重复计费。"""
    keep = keep_recent_screenshots()
    if keep < 0:
        return handler(request)
    messages, dropped = trim_messages_screenshots(request.messages, keep)
    if not dropped:
        return handler(request)
    logger.info(f"[vision]本次只保留最近 {keep} 张截图，已省略历史图片 {dropped} 张")
    return handler(request.override(messages=messages))