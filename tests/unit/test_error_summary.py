"""错误摘要的外流边界（Codex review M4）。

守护需求：REQ-011 / CT-005 —— 响应体不得含堆栈、SQL、连接串、密钥、下游异常原文。

`jobs.result` 在 `ERROR` 状态下会**原样回给调用方**，所以它是「允许外流的文本」的边界。
本文件把这个边界钉死：本系统自己写的文案可以出去，外来异常的原文一律留在日志里。

用例编号：TC-U-026。
"""

from __future__ import annotations

import pytest

from amr.adapters.memory_repository import MemoryJobRepository
from amr.adapters.repository_publisher import RepositoryEventPublisher
from amr.domain.errors import (
    FlowExecutionError,
    IllegalTransitionError,
    RepositoryUnavailableError,
    SchemaValidationError,
)
from amr.domain.inputs import CrewInputs
from amr.domain.status import JobStatus
from amr.services.job_service import JobService, _safe_summary
from tests.fakes import FrozenClock, RecordingTaskQueue, SeqIdGenerator

INPUTS = CrewInputs(
    customer_domain="openai.com",
    project_description="为一家 AI 基础设施公司制定新品发布营销方案",
)

#: 这些是「如果出现在 result 里就说明泄露了」的东西
LEAK_MARKERS = ("sk-", "password", "mysql://", "https://", "Traceback", "SELECT ")


@pytest.fixture
def service() -> JobService:
    repository = MemoryJobRepository()
    clock = FrozenClock()
    return JobService(
        repository=repository,
        queue=RecordingTaskQueue(),
        clock=clock,
        ids=SeqIdGenerator(),
        publisher=RepositoryEventPublisher(repository=repository, clock=clock),
    )


# ------------------------------------------------------------------ TC-U-026


def test_amr_error_message_is_kept() -> None:
    """TC-U-026 本系统自己写的文案要保留：它本来就是为了给人看（REQ-011）。"""
    error = FlowExecutionError("流程执行失败：ConnectionError")

    assert _safe_summary(error) == "流程执行失败：ConnectionError"


def test_schema_error_carries_whitelisted_fields() -> None:
    """TC-U-026 Schema 校验失败的**字段名**是白名单信息，排障必需，允许外流（REQ-006）。"""
    error = SchemaValidationError(
        "结构化结果校验失败",
        context={"fields": ["strategy.tactics", "copy.body"]},
    )

    summary = _safe_summary(error)

    assert "strategy.tactics" in summary
    assert "copy.body" in summary


def test_schema_error_ignores_non_whitelisted_context() -> None:
    """TC-U-026 不在白名单里的 context 字段不得被带出去（REQ-011）。

    `AmrError.__str__` 会把整个 context 拼进字符串——那是给日志用的。
    进 `result` 的必须是筛过的。
    """
    error = SchemaValidationError(
        "结构化结果校验失败",
        context={"raw": "https://gateway.internal/v1?key=sk-secret", "fields": ["copy.body"]},
    )

    summary = _safe_summary(error)

    assert "sk-secret" not in summary
    assert "gateway.internal" not in summary


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("连不上 https://gateway.internal/v1，密钥 sk-abcdef123456"),
        ValueError("SELECT * FROM jobs WHERE password='hunter2'"),
        ConnectionError("mysql://root:hunter2@10.0.0.1:3306/amr 拒绝连接"),
    ],
)
def test_foreign_exception_text_never_leaks(error: BaseException) -> None:
    """TC-U-026 **外来异常只留类型名，原文一律不外流**（REQ-011 / CT-005）。

    下游异常的原文可能带网关 URL、请求参数甚至密钥。这些文本照常进日志（排障用），
    但绝不能经 `result` 回给调用方。
    """
    summary = _safe_summary(error)

    assert type(error).__name__ in summary
    for marker in LEAK_MARKERS:
        assert marker not in summary, f"摘要里出现了 {marker!r}：{summary}"


def test_finish_error_stores_sanitized_summary(service: JobService) -> None:
    """TC-U-026 端到端：`finish_error` 落库的 result 是筛过的（REQ-004 / REQ-011）。"""
    job_id = service.create(INPUTS)
    service.mark_running(job_id)

    job = service.finish_error(
        job_id,
        RuntimeError("调用 https://gateway.internal/v1 失败，api_key=sk-abcdef123456"),
    )

    assert job.status is JobStatus.ERROR
    assert job.result is not None
    assert "RuntimeError" in job.result
    for marker in LEAK_MARKERS:
        assert marker not in job.result


def test_finish_error_keeps_repository_failure_explainable(service: JobService) -> None:
    """TC-U-026 存储故障的摘要是我们自己的文案，仍然可读（REQ-011）。"""
    job_id = service.create(INPUTS)
    service.mark_running(job_id)

    job = service.finish_error(job_id, RepositoryUnavailableError("存储不可用（transition）"))

    assert job.result == "存储不可用（transition）"


def test_illegal_transition_message_is_not_context_dump() -> None:
    """TC-U-026 `IllegalTransitionError` 的 context 里有 job_id 等，但不该整串外流（REQ-004）。"""
    error = IllegalTransitionError(
        "非法的状态迁移",
        context={
            "job_id": "job-1",
            "from": "ERROR",
            "to": "COMPLETE",
            "guard": "conditional_update",
        },
    )

    summary = _safe_summary(error)

    assert summary == "非法的状态迁移"
    assert "guard=" not in summary


def test_flow_result_validation_message_carries_no_foreign_text() -> None:
    """TC-U-026 `FlowResult` 校验失败的 message 不得夹带 pydantic 原文（REQ-006 / CT-005）。

    复核发现：message 里嵌 `{exc}` 时，pydantic 会把它收到的 `input_value=…`
    （模型输出的片段）与 errors.pydantic.dev 链接一起塞进去——而 message 是
    进 `result` 的通道。现在原文只进 context 的 `detail`（非白名单键，只进日志）。
    """
    import json

    from amr.domain.errors import SchemaValidationError
    from amr.domain.models import FlowResult

    broken = {
        "research_report": "x",
        "strategy": {
            "name": "n",
            "tactics": "不是数组，这里可能夹带模型原文 sk-secret",
            "channels": [],
            "kpis": [],
        },
        "campaign_idea": {"name": "n", "description": "d", "audience": "a", "channel": "c"},
        "copy": {"title": "t", "body": "b"},
    }

    with pytest.raises(SchemaValidationError) as exc:
        FlowResult.from_json(json.dumps(broken, ensure_ascii=False))

    summary = _safe_summary(exc.value)

    assert "strategy.tactics" in summary  # 字段名要保留：排障必需
    assert "sk-secret" not in summary
    assert "errors.pydantic.dev" not in summary
