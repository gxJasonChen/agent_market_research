"""把执行期事件写进作业流水（spec/021 §6）。

一个刻意的选择：**这里的异常被吞掉，只记日志**。
事件流水是可观测性设施，它坏掉不应该让一次已经成功的推理变成失败作业（REQ-005）。
"""

from __future__ import annotations

import logging

from amr.ports.clock import Clock
from amr.ports.events import EventSink
from amr.ports.repository import JobRepository

__all__ = ["RepositoryEventPublisher"]

logger = logging.getLogger(__name__)


class _BoundSink:
    """已绑定 `job_id` 的事件出口：`FlowRunner` 只认识它，不知道存储的存在。"""

    def __init__(self, publisher: RepositoryEventPublisher, job_id: str) -> None:
        self._publisher = publisher
        self._job_id = job_id

    def emit(self, data: str) -> None:
        self._publisher.publish(self._job_id, data)


class RepositoryEventPublisher:
    """`EventPublisher` 的实现：事件落到仓储。"""

    def __init__(self, repository: JobRepository, clock: Clock) -> None:
        self._repository = repository
        self._clock = clock

    def publish(self, job_id: str, data: str) -> None:
        try:
            self._repository.append_event(job_id, data, at=self._clock.now())
        except Exception:  # noqa: BLE001 —— 见模块 docstring：吞掉是设计，不是疏忽
            logger.exception("写入事件失败，已忽略以免影响作业本身 job_id=%s", job_id)

    def sink(self, job_id: str) -> EventSink:
        return _BoundSink(self, job_id)
