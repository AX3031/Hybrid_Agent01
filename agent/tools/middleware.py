from typing import Callable
from utils.prompt_loader import load_system_prompts,load_report_prompts
from langchain.agents import AgentState
from langchain.agents.middleware import wrap_tool_call, before_model, dynamic_prompt, ModelRequest
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from utils.logger_handler import logger
from utils.audit_logger import log_tool_call
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
    logger.info(f"[tool monitor]工具{tool_name}传入参数:{tool_args}")

    try:
        result = handler(request)
        logger.info(f"[tool monitor]工具{tool_name}调用成功")

        if tool_name == 'fill_context_for_report':
            request.runtime.context["report"] = True

        content = getattr(result, "content", result)
        status = getattr(result, "status", "success")
        log_tool_call(session_id, tool_name, tool_args, str(content), str(status))
        return result
    except Exception as e:
        log_tool_call(session_id, tool_name, tool_args, str(e), "error")
        logger.error(f"工具{tool_name}调用失败，原因：{str(e)}")
        raise e


@before_model
def log_before_model(
        state: AgentState,          # 整个Agent智能体中的状态记录
        runtime: Runtime,           # 记录了整个执行过程中的上下文信息
):          # 在模型执行前输出日志
    logger.info(f"[log_before_model]即将调用模型，带有{len(state['messages'])}条消息。")

    logger.debug(f"[log_before_model]{type(state['messages'][-1]).__name__} | {state['messages'][-1].content.strip()}")

    return None


@dynamic_prompt         # 每一次在生成提示词之前，调用此函数
def report_prompt_switch(request: ModelRequest):            # 动态切换提示词
    is_report = request.runtime.context.get("report",False)
    if is_report:           # 是报告生成场景，返回报告生成提示词内容
        return load_report_prompts()

    return load_system_prompts()


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