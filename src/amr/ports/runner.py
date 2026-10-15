"""执行端口（spec/021-contracts-ports.md §3）。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from amr.domain.inputs import CrewInputs
from amr.domain.models import FlowResult
from amr.ports.events import EventSink

__all__ = ["FlowExecutor", "FlowRunner"]


@runtime_checkable
class FlowRunner(Protocol):
    """跑一次完整业务流程（调研 → 创作）。

    实现：`adapters.crewai_runner.CrewAIFlowRunner`（生产）、
    `adapters.fake_runner.FakeFlowRunner`（测试）。
    """

    def run(self, job_id: str, inputs: CrewInputs, sink: EventSink) -> FlowResult:
        """同步执行，返回结构化结果。

        必须：
        - 每个 Task 完成时通过 `sink.emit()` 写其原始产出（REQ-005）
        - 模型输出不合 Schema 时抛 `SchemaValidationError`（REQ-006）
        - 任何下游异常包装为 `FlowExecutionError` 抛出（不吞异常）

        不得：直接读写数据库、直接改作业状态——那是 `JobService` 的职责。
        """
        ...


@runtime_checkable
class FlowExecutor(Protocol):
    """Worker 侧的一次执行单元：把作业从「已入队」推进到「终态已落库」。"""

    def execute(self, job_id: str, inputs: CrewInputs) -> None:
        """见 spec/021 §3.1 的责任边界：异常分支 `finish_error` 后**必须 return**。"""
        ...
