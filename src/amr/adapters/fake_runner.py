"""离线确定性执行器（spec/021 §4.1）。

`AMR_LLM_PROVIDER=fake` 时，`worker/bootstrap` 用它替换 `CrewAIFlowRunner`。

它替换的是**执行器**而不是模型：CrewAI 的 `LLM` 会把 `call` 委托给 provider 实现，
桩模型拦不住（详见 spec/021 §4.1 的实测记录）。与其拼一个「假模型」让测试去测桩本身，
不如把这条路明确成「不跑 CrewAI 的确定性执行器」——它覆盖的业务链路同样是真实的：
HTTP → JobService → Celery → Worker → FlowExecutor → Repository。

**它不覆盖** CrewAI 的真实行为，那由真实网关的系统测试（TC-S-004）负责。
"""

from __future__ import annotations

from amr.domain.inputs import CrewInputs
from amr.domain.models import CampaignIdea, Copy, FlowResult, MarketStrategy
from amr.ports.events import EventSink

__all__ = ["FakeFlowRunner"]


class FakeFlowRunner:
    """确定性执行器：同样的输入永远得到同样的结果，不联网、不花钱、毫秒级返回。"""

    def __init__(self, *, emit_task_events: bool = True) -> None:
        self._emit_task_events = emit_task_events

    def run(
        self,
        job_id: str,  # noqa: ARG002 —— FlowRunner 协议的入参，确定性路径用不到它
        inputs: CrewInputs,
        sink: EventSink,
    ) -> FlowResult:
        sink.emit("市场调研阶段开始")
        report = (
            f"（离线确定性产出）针对 {inputs.customer_domain} 的调研报告。"
            f"项目背景：{inputs.project_description}"
        )
        if self._emit_task_events:
            sink.emit(f"[research_task] {report}")
        sink.emit("市场调研阶段完成")

        strategy = MarketStrategy(
            name=f"{inputs.customer_domain} 开发者心智占领",
            tactics=["开源 SDK", "技术博客", "社区运营"],
            channels=["GitHub", "掘金", "技术大会"],
            kpis=["Star 增速", "注册转化率", "留存率"],
        )
        campaign = CampaignIdea(
            name=f"{inputs.customer_domain} 48 小时黑客松",
            description="面向独立开发者的线上黑客松，用真实项目验证接入成本",
            audience="独立开发者与初创团队",
            channel="线上社区",
        )
        copy = Copy(
            title=f"把 {inputs.customer_domain} 接进你的项目，只要三行代码",
            body="从注册到跑通第一个请求不超过 10 分钟，成本立省 60%。",
        )

        sink.emit("内容创作阶段开始")
        if self._emit_task_events:
            sink.emit(f"[marketing_strategy_task] {strategy.model_dump_json()}")
            sink.emit(f"[campaign_idea_task] {campaign.model_dump_json()}")
            sink.emit(f"[copy_creation_task] {copy.model_dump_json()}")
        sink.emit("内容创作阶段完成")

        return FlowResult(
            research_report=report,
            strategy=strategy,
            campaign_idea=campaign,
            copy=copy,
        )
