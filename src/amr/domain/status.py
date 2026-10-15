"""作业状态机。

守护需求 REQ-004：终态不可变、失败优先。
规则只有这一份实现，`JobService`（应用层）与各 `JobRepository`（存储层）都调用它，
避免「两处各写一份迁移判断」而互相不知道对方——源项目的缺陷正是这么来的。
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "ALLOWED_TRANSITIONS",
    "TERMINAL",
    "JobStatus",
    "can_transition",
    "is_terminal",
]


class JobStatus(str, Enum):
    """作业状态。取值即 HTTP 契约字面量（spec/022 CT-002）。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    ERROR = "ERROR"

    def __str__(self) -> str:
        # 3.11 起 mixin 枚举的 str() 行为有分歧，这里钉死成契约字面量，日志里可直接读
        return self.value


#: 终态：一旦进入，任何后续写入都必须被拒绝（REQ-004）
TERMINAL: frozenset[JobStatus] = frozenset({JobStatus.COMPLETE, JobStatus.ERROR})

#: 合法迁移表（spec/020 §1.1）。终态没有出边。
ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PENDING: frozenset({JobStatus.RUNNING, JobStatus.ERROR}),
    JobStatus.RUNNING: frozenset({JobStatus.COMPLETE, JobStatus.ERROR}),
    JobStatus.COMPLETE: frozenset(),
    JobStatus.ERROR: frozenset(),
}


def is_terminal(status: JobStatus) -> bool:
    """是否处于终态。"""
    return status in TERMINAL


def can_transition(src: JobStatus, dst: JobStatus) -> bool:
    """判断 `src → dst` 是否合法。

    判定顺序即语义优先级（spec/020 §1.2）：

    1. `src` 是终态 → 拒绝。**包括写回自身**：终态不可变，重复写终态会让结果被悄悄改写，
       而源项目的「失败作业被误报成功」正是从这一类写入里长出来的。
    2. `src is dst` → 允许。非终态重复写同一状态是幂等（例如 Worker 重投递），不算违规。
    3. 其余查表。
    """
    if is_terminal(src):
        return False
    if src is dst:
        return True
    return dst in ALLOWED_TRANSITIONS[src]
