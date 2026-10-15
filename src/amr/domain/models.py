"""领域模型（spec/020-contracts-domain.md §2）。

分三类：
- **模型输出 Schema**：`MarketStrategy` / `CampaignIdea` / `Copy`，用 pydantic，
  直接交给 CrewAI 的 `output_json` 约束模型输出（REQ-006）。
- **执行结果**：`FlowResult`，聚合两阶段产出，负责 `jobs.result` 的 JSON 形状。
- **值对象**：`Event` / `Job`，用 frozen dataclass——它们是内部结构，
  不参与 HTTP 编解码（HTTP 形状见 spec/022），不需要 pydantic 的解析开销。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from amr.domain.errors import SchemaValidationError
from amr.domain.status import JobStatus

__all__ = [
    "CampaignIdea",
    "Copy",
    "Event",
    "FlowResult",
    "Job",
    "MarketStrategy",
]

# forbidden extras：模型多吐出字段时宁可失败，也不要静默吞掉（REQ-006）
_STRICT = ConfigDict(extra="forbid", frozen=True)


class MarketStrategy(BaseModel):
    """营销战略。"""

    model_config = _STRICT

    name: str = Field(description="市场战略名称")
    tactics: list[str] = Field(description="市场战略中使用的战术清单")
    channels: list[str] = Field(description="市场战略中使用的渠道清单")
    kpis: list[str] = Field(description="关键绩效指标清单")


class CampaignIdea(BaseModel):
    """活动创意。"""

    model_config = _STRICT

    name: str = Field(description="活动创意名称")
    description: str = Field(description="活动创意说明")
    audience: str = Field(description="目标受众")
    channel: str = Field(description="投放渠道")


class Copy(BaseModel):
    """广告文案。"""

    model_config = _STRICT

    title: str = Field(description="标题")
    body: str = Field(description="正文")


@dataclass(frozen=True, slots=True)
class Event:
    """作业流水中的一条事件。"""

    timestamp: datetime
    data: str


@dataclass(frozen=True, slots=True)
class Job:
    """一个作业的完整快照。不可变：改状态由仓储产生新值。"""

    job_id: str
    status: JobStatus
    customer_domain: str
    project_description: str
    result: str | None
    created_at: datetime
    updated_at: datetime
    events: tuple[Event, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class FlowResult:
    """一次 Flow 执行的完整产出，即 `COMPLETE` 状态下 `jobs.result` 的 JSON 形状。"""

    research_report: str
    strategy: MarketStrategy
    campaign_idea: CampaignIdea
    copy: Copy

    def to_json(self) -> str:
        """序列化为 `jobs.result` 的 JSON 文本（spec/020 §2.3）。"""
        return json.dumps(
            {
                "research_report": self.research_report,
                "strategy": self.strategy.model_dump(mode="json"),
                "campaign_idea": self.campaign_idea.model_dump(mode="json"),
                "copy": self.copy.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str | Mapping[str, Any]) -> FlowResult:
        """反序列化并**校验**。任何不合 Schema 的输入都抛 `SchemaValidationError`。

        这里是「模型输出不可信」的落点：只有过了这一关的数据才允许进入 `jobs.result`。
        """
        payload: Any = raw
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise SchemaValidationError(
                    f"结果不是合法 JSON：{exc.msg}",
                    context={"position": exc.pos},
                ) from exc

        if not isinstance(payload, Mapping):
            raise SchemaValidationError(
                "结果顶层必须是 JSON 对象",
                context={"actual_type": type(payload).__name__},
            )

        report = payload.get("research_report")
        if not isinstance(report, str) or not report:
            raise SchemaValidationError(
                "结果缺少 research_report",
                context={"field": "research_report"},
            )

        return cls(
            research_report=report,
            strategy=_validate(MarketStrategy, payload.get("strategy"), "strategy"),
            campaign_idea=_validate(CampaignIdea, payload.get("campaign_idea"), "campaign_idea"),
            copy=_validate(Copy, payload.get("copy"), "copy"),
        )


def _validate[ModelT: BaseModel](model: type[ModelT], value: Any, key: str) -> ModelT:
    """校验子模型，并把顶层字段名拼进错误定位。

    没有这一层，错误里只有 `tactics` —— 但 `tactics` 在战略和创意里都可能出现，
    排障时无法判断坏的是哪一段（REQ-006）。
    """
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        fields = sorted(
            f"{key}.{'.'.join(str(part) for part in err['loc'])}" for err in exc.errors()
        )
        raise SchemaValidationError(
            f"结构化结果校验失败，字段：{', '.join(fields) or key}；{exc}",
            context={"fields": fields},
        ) from exc
