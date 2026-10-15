"""时间与标识端口（spec/021-contracts-ports.md §5）。

业务代码**不得**直接 `datetime.now()` / `uuid4()`：那样测试无法复现，
断言只能写成「大概是现在」这种不疼不痒的样子。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "IdGenerator"]


@runtime_checkable
class Clock(Protocol):
    """时间源。"""

    def now(self) -> datetime:
        """带时区的当前时间（UTC）。"""
        ...


@runtime_checkable
class IdGenerator(Protocol):
    """标识生成器。"""

    def new_id(self) -> str:
        """新的作业 ID。生产实现为 UUID4 字符串。"""
        ...
