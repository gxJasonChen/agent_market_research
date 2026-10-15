"""事件端口（spec/021-contracts-ports.md §3 / §6）。

两个概念不要混：
- `EventSink`：**执行期句柄**，已绑定好 `job_id`，`FlowRunner` 只认识它，不知道存储存在。
- `EventPublisher`：**落点**，负责把事件写进作业流水；`sink(job_id)` 造出前者。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["EventPublisher", "EventSink"]


@runtime_checkable
class EventSink(Protocol):
    """绑定到某个作业的事件出口。"""

    def emit(self, data: str) -> None:
        """写一条事件。实现**不得**向上抛异常——可观测性故障不该拖垮业务（REQ-005）。"""
        ...


@runtime_checkable
class EventPublisher(Protocol):
    """事件落点。"""

    def publish(self, job_id: str, data: str) -> None:
        """把事件写进指定作业的流水。"""
        ...

    def sink(self, job_id: str) -> EventSink:
        """返回绑定到 `job_id` 的 `EventSink`。"""
        ...
