"""HTTP 编解码形状（spec/022）。

**请求体不在这里定义**：`POST /api/crew` 直接用 `amr.domain.inputs.CrewInputs`，
校验规则只有那一份（REQ-010）。这里只声明响应形状，以及
「存储里的作业 → CT-002 响应体」的翻译。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from amr.domain.models import Event, Job
from amr.domain.status import JobStatus

__all__ = [
    "HealthResponse",
    "JobEventOut",
    "JobResponse",
    "SubmitResponse",
    "to_job_response",
]

logger = logging.getLogger(__name__)


class SubmitResponse(BaseModel):
    """CT-001 响应体。"""

    job_id: str = Field(description="作业 ID（UUID4）")


class JobEventOut(BaseModel):
    """CT-002 的一条事件。"""

    timestamp: str = Field(description="ISO-8601 带时区偏移")
    data: str = Field(description="事件内容")


class JobResponse(BaseModel):
    """CT-002 响应体。"""

    job_id: str
    # 刻意是 str 而不是枚举：契约规定新增状态不算破坏性变更，前端按字符串容错（CT-002）
    status: str = Field(description="PENDING / RUNNING / COMPLETE / ERROR")
    result: dict[str, Any] | str | None = Field(
        default=None,
        description="COMPLETE → 结构化对象；ERROR → 错误摘要字符串；PENDING/RUNNING → null",
    )
    events: list[JobEventOut] = Field(default_factory=list, description="按 timestamp 正序")


class HealthResponse(BaseModel):
    """CT-003 响应体。只反映进程存活，不反映下游依赖健康。"""

    status: str
    version: str


def to_job_response(job: Job) -> JobResponse:
    """把作业快照翻译成 CT-002 的响应体。

    三条规则集中在这里，路由只负责取数：

    - `status` 原样透出（`JobStatus` 是 str 枚举，取值即契约字面量）；
    - `result` 只在 `COMPLETE` 时是结构化对象，`ERROR` 时保持字符串，其余为 `null`（REQ-006）；
    - `events` 按 timestamp 正序——仓储已保证，这里再排一次：契约要求不能依赖实现的偶然，
      而且 `sorted` 是稳定的，同一时刻写入的事件仍按写入顺序排列。
    """
    return JobResponse(
        job_id=job.job_id,
        status=job.status.value,
        result=_translate_result(job),
        events=[_to_event(event) for event in sorted(job.events, key=lambda item: item.timestamp)],
    )


def _to_event(event: Event) -> JobEventOut:
    # 用 isoformat() 而不是让 pydantic 代劳：把线上格式钉在一处（带时区偏移，CT-022）
    return JobEventOut(timestamp=event.timestamp.isoformat(), data=event.data)


def _translate_result(job: Job) -> dict[str, Any] | str | None:
    if job.status is JobStatus.COMPLETE:
        return _parse_structured(job)
    if job.status is JobStatus.ERROR:
        return job.result
    return None


def _parse_structured(job: Job) -> dict[str, Any] | None:
    """解析 `COMPLETE` 作业的结构化结果。

    解析不出来时记 ERROR 日志并返回 `null`：宁可少给数据，也**不**把裸文本塞进
    `result` 冒充结构化结果（CT-002 语义保证 1 / REQ-006）。
    """
    if job.result is None:
        logger.error("作业状态为 COMPLETE 但没有结果 job_id=%s", job.job_id)
        return None
    try:
        payload = json.loads(job.result)
    except json.JSONDecodeError:
        logger.error("作业结果为非法 JSON，无法结构化返回 job_id=%s", job.job_id)
        return None
    if not isinstance(payload, dict):
        logger.error("作业结果不是 JSON 对象 job_id=%s", job.job_id)
        return None
    return payload
