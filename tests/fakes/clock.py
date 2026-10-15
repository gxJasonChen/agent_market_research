"""时间与 ID 的确定性替身（spec/021 §5）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

__all__ = ["FrozenClock", "SeqIdGenerator"]

DEFAULT_START = datetime(2026, 10, 15, 10, 0, 0, tzinfo=UTC)


class FrozenClock:
    """冻结时钟：`now()` 恒定，可手动推进，断言里能写死时间。"""

    def __init__(self, start: datetime = DEFAULT_START) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float = 1.0) -> datetime:
        """推进时间，用于制造「事件之间有先后」的场景。"""
        self._now += timedelta(seconds=seconds)
        return self._now


class SeqIdGenerator:
    """自增 ID：产出 `job-1`、`job-2`…，断言可写死。"""

    def __init__(self, prefix: str = "job") -> None:
        self._prefix = prefix
        self._counter = 0

    def new_id(self) -> str:
        self._counter += 1
        return f"{self._prefix}-{self._counter}"
