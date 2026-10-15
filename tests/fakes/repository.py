"""记录型仓储替身：包住真实实现，把调用序列记下来。

用途（spec/040 TC-U-013）：断言「异常路径之后**从未**出现 COMPLETE 写入」——
这是源项目缺陷最容易复发的形状，光看最终状态是不够的。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from amr.adapters.memory_repository import MemoryJobRepository
from amr.domain.models import Job
from amr.domain.status import JobStatus

__all__ = ["RecordingJobRepository"]


class RecordingJobRepository:
    """委托给 `MemoryJobRepository`，并记录每一次写操作。"""

    def __init__(self, inner: MemoryJobRepository | None = None) -> None:
        self._inner = inner or MemoryJobRepository()
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    # ---- 记录 ----

    @property
    def transitions(self) -> list[JobStatus]:
        """按顺序返回所有成功写入的目标状态。"""
        return [args[1] for name, args, _ in self.calls if name == "transition"]

    @property
    def events(self) -> list[str]:
        """按顺序返回所有写入的事件文本。"""
        return [args[1] for name, args, _ in self.calls if name == "append_event"]

    # ---- JobRepository ----

    def create(self, job: Job) -> None:
        self.calls.append(("create", (job,), {}))
        self._inner.create(job)

    def get(self, job_id: str) -> Job | None:
        return self._inner.get(job_id)

    def append_event(self, job_id: str, data: str, *, at: datetime) -> None:
        self.calls.append(("append_event", (job_id, data), {"at": at}))
        self._inner.append_event(job_id, data, at=at)

    def transition(
        self,
        job_id: str,
        to: JobStatus,
        *,
        result: str | None = None,
        at: datetime,
    ) -> Job:
        # 只在**成功**后记录，失败的迁移不该出现在序列里
        job = self._inner.transition(job_id, to, result=result, at=at)
        self.calls.append(("transition", (job_id, to), {"result": result, "at": at}))
        return job

    def list_recent(self, limit: int = 20) -> list[Job]:
        return self._inner.list_recent(limit)
