"""测试用的假模型：不联网、不花钱，按脚本决定“调工具”还是“直接回答”。"""

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ScriptedToolModel(BaseChatModel):
    """前 ``tool_turns`` 轮返回工具调用，之后返回最终回答。

    这样可以在同一个会话里模拟“先失败、再继续”的多轮流程，
    且不依赖真实模型与网络。
    """

    tool_name: str = "boom"
    tool_args: dict[str, Any] = {}
    tool_turns: int = 1
    final_text: str = "已完成"
    tool_turns_used: int = 0
    call_seq: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-tool-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedToolModel":
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        # 一轮 = 一次模型调用；工具结果回来后如果还有额度就再调一次工具
        if isinstance(messages[-1], ToolMessage) or self.tool_turns_used >= self.tool_turns:
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content=self.final_text))]
            )

        self.tool_turns_used += 1
        self.call_seq += 1
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": self.tool_name,
                                "args": dict(self.tool_args),
                                "id": f"call_{self.call_seq}",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )
