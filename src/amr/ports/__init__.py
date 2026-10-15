"""端口层：依赖倒置的接缝（spec/021）。

**冻结面**：这些签名是并行开发各方共同的约定，改签名 = 破坏性变更。
`services/` 只能 import 本包与 `amr.domain`。
"""

from amr.ports.clock import Clock, IdGenerator
from amr.ports.events import EventPublisher, EventSink
from amr.ports.llm import ChatModel, LLMProvider
from amr.ports.queue import TaskQueue
from amr.ports.repository import JobRepository
from amr.ports.runner import FlowExecutor, FlowRunner

__all__ = [
    "ChatModel",
    "Clock",
    "EventPublisher",
    "EventSink",
    "FlowExecutor",
    "FlowRunner",
    "IdGenerator",
    "JobRepository",
    "LLMProvider",
    "TaskQueue",
]
