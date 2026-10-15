"""模型端口（spec/021-contracts-ports.md §4）。

业务层只认识 `ChatModel` 这个结构性协议；`crewai.LLM` 天然满足它，
无需继承、无需注册。CrewAI 升级不会波及 `services/`。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["ChatModel", "LLMProvider"]


@runtime_checkable
class ChatModel(Protocol):
    """CrewAI Agent 可用的对话模型。"""

    def call(
        self,
        messages: Any,
        tools: Any = None,
        callbacks: Any = None,
        **kwargs: Any,
    ) -> Any:
        """发起一次对话，返回模型回复。"""
        ...

    def supports_function_calling(self) -> bool:
        """是否支持工具调用（决定 Agent 能否挂工具）。"""
        ...


@runtime_checkable
class LLMProvider(Protocol):
    """按配置构造模型客户端。实现见 `adapters.llm_factory`。"""

    def build(self) -> ChatModel:
        """构造模型。缺少必填配置时抛 `InvalidInputError`，信息里**只点名变量名、不带值**。"""
        ...
