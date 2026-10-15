"""存储端口（spec/021-contracts-ports.md §1）。

实现：`adapters.memory_repository.MemoryJobRepository`（测试/集成）、
`adapters.mysql_repository.MySQLJobRepository`（生产）。
两者必须通过**同一套**契约测试（tests/contract）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from amr.domain.models import Job
from amr.domain.status import JobStatus

__all__ = ["JobRepository"]


@runtime_checkable
class JobRepository(Protocol):
    """作业与事件的持久化。"""

    def create(self, job: Job) -> None:
        """插入作业。`job_id` 已存在时抛 `ValueError`。

        调用方负责把状态置为 `PENDING` 并写首条事件——仓储不替业务做决定。
        """
        ...

    def get(self, job_id: str) -> Job | None:
        """返回作业及其事件（按时间正序）。不存在返回 `None`，**不抛异常**。"""
        ...

    def append_event(self, job_id: str, data: str, *, at: datetime) -> None:
        """追加一条事件。作业不存在抛 `JobNotFoundError`（不隐式建作业）。"""
        ...

    def transition(
        self,
        job_id: str,
        to: JobStatus,
        *,
        result: str | None = None,
        at: datetime,
    ) -> Job:
        """状态迁移，返回迁移后的作业快照。

        - 作业不存在 → `JobNotFoundError`
        - 迁移非法（含写入终态）→ `IllegalTransitionError`，**且库中状态保持不变**
        - 同一非终态重复写入 → 幂等成功（`result` 若传入则更新）
        - 存储不可用 → `RepositoryUnavailableError`

        实现必须在存储层再做一次并发防护（MySQL：条件 UPDATE + affected rows 校验），
        否则两个 Worker 并发收尾时，终态仍可能被覆盖（REQ-004）。
        """
        ...

    def list_recent(self, limit: int = 20) -> list[Job]:
        """按 `created_at` 倒序返回最近作业（不含 events，避免 N+1 查询）。"""
        ...
