"""CrewAI Flow 编排（spec/010 §4.3、REQ-007）。

两阶段串成一条 Flow：`@start` 跑市场调研 Crew，`@listen` 用它的产出跑内容创作 Crew。
Crew 之间靠 Flow 传递，而不是在一个 Crew 里堆任务——阶段边界清楚，也便于单独替换。

本模块是**编排细节的边界**：`services/` 只认识 `FlowRunner` 协议，
CrewAI 怎么变都在这一层消化掉。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from crewai.flow import Flow, listen, start

from amr.crews._output import output_text, structured_output
from amr.crews.content_creator.crew import (
    TASK_CAMPAIGN,
    TASK_COPY,
    TASK_STRATEGY,
    ContentCreatorCrew,
)
from amr.crews.market_analyst.crew import MarketAnalystCrew
from amr.domain.errors import FlowExecutionError, SchemaValidationError
from amr.domain.inputs import CrewInputs
from amr.domain.models import CampaignIdea, Copy, FlowResult, MarketStrategy
from amr.ports.events import EventSink

__all__ = ["CrewAIFlowRunner"]

logger = logging.getLogger(__name__)

EVENT_RESEARCH_START = "市场调研阶段开始"
EVENT_RESEARCH_DONE = "市场调研阶段完成"
EVENT_CONTENT_START = "内容创作阶段开始"
EVENT_CONTENT_DONE = "内容创作阶段完成"


class _TwoStageFlow(Flow[dict[str, Any]]):
    """调研 → 创作的链式 Flow。

    输入、模型与事件出口都由构造函数注入：Flow 的 `kickoff(inputs=...)` 面向的是
    「可序列化的流程参数」，而模型对象与 sink 不是数据，放进去只会徒增麻烦。
    """

    def __init__(
        self,
        *,
        model: Any,
        sink: EventSink,
        inputs: CrewInputs,
        search_tools: Sequence[Any] | None,
    ) -> None:
        super().__init__()
        self._model = model
        self._sink = sink
        self._inputs = inputs
        self._search_tools = search_tools
        self.result: FlowResult | None = None

    @start()
    def market_analyst(self) -> str:
        """阶段一：产出调研报告，作为阶段二的输入。"""
        self._sink.emit(EVENT_RESEARCH_START)
        crew = MarketAnalystCrew(
            self._model,
            tools=self._search_tools,
            on_task_complete=self._on_task,
        )
        report = crew.kickoff(self._inputs)
        self._sink.emit(EVENT_RESEARCH_DONE)
        return report

    @listen(market_analyst)
    def content_creator(self, research_report: str) -> str:
        """阶段二：以调研结论为上下文，产出战略、创意与文案。"""
        self._sink.emit(EVENT_CONTENT_START)
        crew = ContentCreatorCrew(
            self._model,
            tools=self._search_tools,
            on_task_complete=self._on_task,
        )
        crew.kickoff(self._inputs)

        self.result = FlowResult(
            research_report=research_report,
            strategy=structured_output(crew.outputs[TASK_STRATEGY], MarketStrategy),
            campaign_idea=structured_output(crew.outputs[TASK_CAMPAIGN], CampaignIdea),
            copy=structured_output(crew.outputs[TASK_COPY], Copy),
        )
        self._sink.emit(EVENT_CONTENT_DONE)
        return research_report

    def _on_task(self, key: str, output: Any) -> None:
        """每个 Task 完成后把原始产出写进流水（REQ-005）。

        事件文本带任务名：排查「Agent 在哪一步跑偏」时，没有任务名的流水基本没用。
        """
        self._sink.emit(f"[{key}] {output_text(output)}")


class CrewAIFlowRunner:
    """`FlowRunner` 的生产实现。"""

    def __init__(self, model: Any, *, search_tools: Sequence[Any] | None = None) -> None:
        self._model = model
        self._search_tools = search_tools

    def run(self, job_id: str, inputs: CrewInputs, sink: EventSink) -> FlowResult:
        """执行两阶段 Flow。

        异常分两类处理：
        - `SchemaValidationError` 原样抛出——它已经点明了哪个模型、哪个字段，
          再包一层只会把有用的信息埋掉（REQ-006）；
        - 其余一律包成 `FlowExecutionError`，并**保留异常链**，排障时能看到根因。
        """
        flow = _TwoStageFlow(
            model=self._model,
            sink=sink,
            inputs=inputs,
            search_tools=self._search_tools,
        )
        try:
            flow.kickoff()
        except SchemaValidationError:
            raise
        except Exception as exc:
            logger.exception("Flow 执行失败 job_id=%s", job_id)
            raise FlowExecutionError(
                f"流程执行失败：{type(exc).__name__}",
                context={"job_id": job_id, "reason": str(exc)[:200]},
            ) from exc

        if flow.result is None:  # pragma: no cover —— 只有 Flow 被中断才会走到
            raise FlowExecutionError("流程结束但未产出结果", context={"job_id": job_id})
        return flow.result
