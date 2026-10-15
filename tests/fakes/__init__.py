"""共享测试替身（spec/040 §4）。

原则：这些是**行为明确**的轻量实现，不是 `MagicMock`。
它们与真实实现共享同一份端口契约，因此不会「假装通过」。
"""

from tests.fakes.clock import FrozenClock, SeqIdGenerator
from tests.fakes.llm import FakeChatModel
from tests.fakes.queues import FailingTaskQueue, RecordingTaskQueue
from tests.fakes.repository import RecordingJobRepository
from tests.fakes.runners import (
    FAILING_MESSAGE,
    FailingFlowRunner,
    FakeFlowRunner,
    SchemaBrokenFlowRunner,
    sample_result,
)

__all__ = [
    "FAILING_MESSAGE",
    "FailingFlowRunner",
    "FailingTaskQueue",
    "FakeChatModel",
    "FakeFlowRunner",
    "FrozenClock",
    "RecordingJobRepository",
    "RecordingTaskQueue",
    "SchemaBrokenFlowRunner",
    "SeqIdGenerator",
    "sample_result",
]
