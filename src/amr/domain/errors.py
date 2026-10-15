"""领域异常（spec/020-contracts-domain.md §4）。

约定：
- 所有业务异常继承 `AmrError`，携带人类可读 `message` 与结构化 `context`。
- 异常信息**不得**包含密钥、连接串、模型 API 原文（spec/000 §7）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "AmrError",
    "FlowExecutionError",
    "IllegalTransitionError",
    "InvalidInputError",
    "JobNotFoundError",
    "QueueUnavailableError",
    "RepositoryUnavailableError",
    "SchemaValidationError",
]


class AmrError(Exception):
    """本系统所有业务异常的基类。"""

    def __init__(self, message: str, *, context: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = dict(context or {})

    def __str__(self) -> str:
        if not self.context:
            return self.message
        detail = ", ".join(f"{key}={value!r}" for key, value in sorted(self.context.items()))
        return f"{self.message} ({detail})"


class InvalidInputError(AmrError):
    """领域层输入校验失败（HTTP 层映射 422）。"""


class JobNotFoundError(AmrError):
    """作业不存在（HTTP 层映射 404）。"""


class IllegalTransitionError(AmrError):
    """非法状态迁移，含「写入终态」（REQ-004）。

    这不是用户错误，而是编程错误或并发冲突，因此映射 500 并**必须**留下日志。
    """


class SchemaValidationError(AmrError):
    """模型输出不符合 Schema（REQ-006）。错误信息必须点名模型与字段。"""


class RepositoryUnavailableError(AmrError):
    """存储不可用（HTTP 层映射 503）。"""


class QueueUnavailableError(AmrError):
    """任务队列不可用（HTTP 层映射 503）。"""


class FlowExecutionError(AmrError):
    """Flow 执行失败，包装下游异常原因。"""
