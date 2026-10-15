"""任务队列端口（spec/021-contracts-ports.md §2）。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from amr.domain.inputs import CrewInputs

__all__ = ["TaskQueue"]


@runtime_checkable
class TaskQueue(Protocol):
    """把作业投递给执行方。

    实现：`adapters.celery_queue.CeleryTaskQueue`（生产）、
    `adapters.inline_queue.InlineTaskQueue`（集成测试：同进程同步执行）。
    """

    def enqueue_flow(self, job_id: str, inputs: CrewInputs) -> None:
        """投递一次 Flow 执行。

        - 投递失败（broker 不可达等）→ 抛 `QueueUnavailableError`。
        - **语义**：投递成功 ≠ 执行成功。
        - 调用方（`JobService.create`）在投递失败时**必须**把作业置为 `ERROR`，
          否则会留下永远停在 `PENDING` 的僵尸作业（REQ-011）。
        """
        ...
