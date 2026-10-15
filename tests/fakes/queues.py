"""`TaskQueue` 替身。"""

from __future__ import annotations

from amr.domain.errors import QueueUnavailableError
from amr.domain.inputs import CrewInputs

__all__ = ["FailingTaskQueue", "RecordingTaskQueue"]


class RecordingTaskQueue:
    """只记录投递，不执行——用于断言「先落库、后入队」的顺序。"""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, CrewInputs]] = []

    def enqueue_flow(self, job_id: str, inputs: CrewInputs) -> None:
        self.enqueued.append((job_id, inputs))


class FailingTaskQueue:
    """投递必失败——用于验证「不留僵尸 PENDING」（REQ-011）。"""

    def __init__(self, message: str = "broker 不可达") -> None:
        self.message = message
        self.attempts: list[str] = []

    def enqueue_flow(self, job_id: str, inputs: CrewInputs) -> None:
        self.attempts.append(job_id)
        raise QueueUnavailableError(self.message, context={"job_id": job_id})
