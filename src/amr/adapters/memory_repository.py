"""`JobRepository` 的内存实现（spec/021 §1）。

不是「测试用的一次性 mock」，而是与 MySQL 实现**同契约**的生产级实现：
同一套契约测试（tests/contract）会在两者上各跑一遍。
用途：单元/集成测试、以及无 MySQL 环境下的本地起服务。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from threading import RLock

from amr.domain.errors import IllegalTransitionError, JobNotFoundError
from amr.domain.models import Event, Job
from amr.domain.status import JobStatus, can_transition, is_terminal

__all__ = ["MemoryJobRepository"]


class MemoryJobRepository:
    """进程内存储，用锁保证多线程下的读写一致。"""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._events: dict[str, list[Event]] = {}
        self._lock = RLock()

    def create(self, job: Job) -> None:
        with self._lock:
            if job.job_id in self._jobs:
                raise ValueError(f"作业已存在: {job.job_id}")
            self._jobs[job.job_id] = replace(job, events=())
            self._events[job.job_id] = []

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return replace(job, events=tuple(self._events.get(job_id, ())))

    def append_event(self, job_id: str, data: str, *, at: datetime) -> None:
        with self._lock:
            if job_id not in self._jobs:
                raise JobNotFoundError("作业不存在，无法追加事件", context={"job_id": job_id})
            self._events[job_id].append(Event(timestamp=at, data=data))

    def transition(
        self,
        job_id: str,
        to: JobStatus,
        *,
        result: str | None = None,
        at: datetime,
        expected_from: JobStatus | None = None,
    ) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFoundError("作业不存在，无法迁移状态", context={"job_id": job_id})
            if not can_transition(job.status, to):
                # 规则只有 domain/status.py 一份，这里只是执行它（REQ-004）
                raise IllegalTransitionError(
                    "非法的状态迁移",
                    context={"job_id": job_id, "from": job.status.value, "to": to.value},
                )
            if expected_from is not None and job.status is not expected_from:
                # 占用语义：状态已经被别人改过了，本次写入作废
                raise IllegalTransitionError(
                    "状态与预期不符，迁移作废",
                    context={
                        "job_id": job_id,
                        "from": job.status.value,
                        "to": to.value,
                        "expected_from": expected_from.value,
                        "guard": "expected_from",
                    },
                )
            if is_terminal(to) and result is None:
                raise IllegalTransitionError(
                    "写入终态必须带 result",
                    context={"job_id": job_id, "to": to.value, "guard": "terminal_requires_result"},
                )
            updated = replace(
                job,
                status=to,
                result=result if result is not None else job.result,
                updated_at=at,
            )
            self._jobs[job_id] = updated
            return replace(updated, events=tuple(self._events.get(job_id, ())))

    def list_recent(self, limit: int = 20) -> list[Job]:
        if limit < 1:
            raise ValueError(f"limit 必须大于 0，收到 {limit}")

        with self._lock:
            # 两个排序键与 MySQL 实现逐字对齐：created_at 倒序，并列时 job_id 升序。
            # 少了第二键，同一时刻创建的作业在两个实现里的顺序会不一致，
            # 契约测试就会变成「各自都对、互相不对」。
            ordered = sorted(
                self._jobs.values(), key=lambda job: (-job.created_at.timestamp(), job.job_id)
            )
            return [replace(job, events=()) for job in ordered[:limit]]
