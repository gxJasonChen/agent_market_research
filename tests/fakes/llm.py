"""`ChatModel` 替身：确定性、零成本、不联网（spec/040 §4）。"""

from __future__ import annotations

from typing import Any

__all__ = ["FakeChatModel"]


class FakeChatModel:
    """满足 `ChatModel` 协议的桩模型。

    同一个 prompt 永远得到同一个回复——这是「单测全 Fake」能成立的前提（NFR-001）。
    """

    def __init__(self, reply: str = "stub-response", *, function_calling: bool = True) -> None:
        self.reply = reply
        self._function_calling = function_calling
        self.calls: list[Any] = []

    def call(
        self,
        messages: Any,
        tools: Any = None,
        callbacks: Any = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append(messages)
        return self.reply

    def supports_function_calling(self) -> bool:
        return self._function_calling
