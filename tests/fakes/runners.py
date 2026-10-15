"""`FlowRunner` 替身。"""

from __future__ import annotations

from collections.abc import Sequence

from amr.domain.errors import FlowExecutionError, SchemaValidationError
from amr.domain.inputs import CrewInputs
from amr.domain.models import CampaignIdea, Copy, FlowResult, MarketStrategy
from amr.ports.events import EventSink

__all__ = ["FAILING_MESSAGE", "FailingFlowRunner", "FakeFlowRunner", "sample_result"]

FAILING_MESSAGE = "调研阶段失败：搜索工具不可用"


def sample_result(report: str = "调研报告：测试用") -> FlowResult:
    """固定结果，便于断言写死具体值而不是 `is not None`。"""
    return FlowResult(
        research_report=report,
        strategy=MarketStrategy(
            name="开发者心智占领",
            tactics=["开源 SDK", "技术博客"],
            channels=["GitHub"],
            kpis=["Star 增速"],
        ),
        campaign_idea=CampaignIdea(
            name="黑客松",
            description="线上黑客松",
            audience="独立开发者",
            channel="线上社区",
        ),
        copy=Copy(title="三行代码接入", body="成本立省 60%。"),
    )


class FakeFlowRunner:
    """返回固定结果的 `FlowRunner`；可配置要发出的事件与要抛的异常。"""

    def __init__(
        self,
        result: FlowResult | None = None,
        *,
        emit: Sequence[str] = ("调研完成", "战略完成"),
        error: Exception | None = None,
    ) -> None:
        self.result = result or sample_result()
        self.emit = list(emit)
        self.error = error
        self.calls: list[tuple[str, CrewInputs]] = []

    def run(self, job_id: str, inputs: CrewInputs, sink: EventSink) -> FlowResult:
        self.calls.append((job_id, inputs))
        if self.error is not None:
            raise self.error
        for message in self.emit:
            sink.emit(message)
        return self.result


class FailingFlowRunner(FakeFlowRunner):
    """总是失败的 Runner（模拟调研阶段抛错）。"""

    def __init__(self, error: Exception | None = None) -> None:
        super().__init__(error=error or FlowExecutionError(FAILING_MESSAGE))


class SchemaBrokenFlowRunner(FakeFlowRunner):
    """模型输出不合 Schema 的 Runner（REQ-006 的失败路径）。"""

    def __init__(self) -> None:
        super().__init__(
            error=SchemaValidationError(
                "模型输出不符合 Schema：战略字段缺失",
                context={"field": "strategy"},
            )
        )
