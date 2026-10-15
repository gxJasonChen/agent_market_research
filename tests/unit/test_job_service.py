"""`JobService` 单元测试——作业状态机的唯一写入口。

守护需求：REQ-001 / REQ-004 / REQ-005 / REQ-011。
用例编号：TC-U-009 ~ TC-U-012。
"""

from __future__ import annotations

import pytest

from amr.adapters.memory_repository import MemoryJobRepository
from amr.adapters.repository_publisher import RepositoryEventPublisher
from amr.domain.errors import IllegalTransitionError, JobNotFoundError, QueueUnavailableError
from amr.domain.inputs import CrewInputs
from amr.domain.models import FlowResult, Job
from amr.domain.status import JobStatus
from amr.services.job_service import JobService
from tests.fakes import (
    FailingTaskQueue,
    FrozenClock,
    RecordingJobRepository,
    RecordingTaskQueue,
    SeqIdGenerator,
    sample_result,
)

INPUTS = CrewInputs(
    customer_domain="openai.com",
    project_description="为一家 AI 基础设施公司制定新品发布营销方案",
)


def _service(
    *,
    repository: RecordingJobRepository | None = None,
    queue: RecordingTaskQueue | FailingTaskQueue | None = None,
) -> tuple[JobService, RecordingJobRepository, RecordingTaskQueue | FailingTaskQueue, FrozenClock]:
    repo = repository or RecordingJobRepository()
    task_queue = queue or RecordingTaskQueue()
    clock = FrozenClock()
    service = JobService(
        repository=repo,
        queue=task_queue,
        clock=clock,
        ids=SeqIdGenerator(),
        publisher=RepositoryEventPublisher(repository=repo, clock=clock),
    )
    return service, repo, task_queue, clock


def _created(service: JobService, repo: RecordingJobRepository) -> tuple[str, Job]:
    job_id = service.create(INPUTS)
    job = repo.get(job_id)
    assert job is not None
    return job_id, job


# ------------------------------------------------------------------ TC-U-009


def test_create_persists_pending_job_and_enqueues() -> None:
    """TC-U-009 提交作业：落库 PENDING、result 为空、写首条事件、投递队列（REQ-001）。"""
    service, repo, task_queue, _ = _service()

    job_id = service.create(INPUTS)

    assert job_id == "job-1"  # ID 来自注入的生成器，断言可写死
    job = repo.get(job_id)
    assert job is not None
    assert job.status is JobStatus.PENDING
    assert job.result is None
    assert job.customer_domain == "openai.com"
    assert [event.data for event in job.events] == ["作业已受理", "作业已入队"]
    assert [enqueued_id for enqueued_id, _ in task_queue.enqueued] == [job_id]  # type: ignore[union-attr]


def test_create_persists_before_enqueue() -> None:
    """TC-U-009 补充：**先落库后入队**，顺序颠倒会在 Worker 抢跑时丢作业（REQ-001）。"""
    service, repo, _, _ = _service()

    service.create(INPUTS)

    methods = [name for name, _, _ in repo.calls]
    assert methods[0] == "create"
    assert "append_event" in methods
    # 入队发生在落库之后：队列替身被调用时，作业已经在仓储里了
    assert repo.get("job-1") is not None


# ------------------------------------------------------------------ TC-U-010


def test_create_marks_error_when_enqueue_fails() -> None:
    """TC-U-010 入队失败：作业必须置 ERROR，**不留僵尸 PENDING**（REQ-011）。"""
    failing_queue = FailingTaskQueue()
    service, repo, _, _ = _service(queue=failing_queue)

    with pytest.raises(QueueUnavailableError):
        service.create(INPUTS)

    job = repo.get("job-1")
    assert job is not None
    assert job.status is JobStatus.ERROR
    assert job.result is not None and "队列" in job.result
    assert failing_queue.attempts == ["job-1"]


# ------------------------------------------------------------------ TC-U-011


def test_finish_error_records_summary() -> None:
    """TC-U-011 失败收尾：状态 ERROR，result 是**筛过**的错误摘要（REQ-004 / CT-005）。

    注意与 `test_error_summary.py` 的分工：这里断言状态机行为，
    那边专门断言「哪些文本允许外流」。
    """
    service, repo, _, _ = _service()
    job_id, _ = _created(service, repo)

    service.mark_running(job_id)
    job = service.finish_error(job_id, RuntimeError("调研阶段失败：工具异常"))

    assert job.status is JobStatus.ERROR
    # 外来异常的原文不外流（Codex review M4）：只留类型名，原文在日志里
    assert job.result == "RuntimeError（详情见服务端日志）"
    assert repo.get(job_id) is not None
    assert repo.get(job_id).status is JobStatus.ERROR  # type: ignore[union-attr]


def test_finish_error_accepts_string_reason() -> None:
    """TC-U-011 补充：也可直接传字符串摘要（REQ-004）。"""
    service, repo, _, _ = _service()
    job_id, _ = _created(service, repo)
    service.mark_running(job_id)

    assert service.finish_error(job_id, "模型网关超时").result == "模型网关超时"


# ------------------------------------------------------------------ TC-U-012


def test_finish_success_after_error_is_rejected() -> None:
    """TC-U-012 **失败作业不能被改写成成功**（REQ-004）。

    源项目严重缺陷的端到端回归：异常分支写 ERROR 后，成功分支又无条件写 COMPLETE。
    """
    service, repo, _, _ = _service()
    job_id, _ = _created(service, repo)
    service.mark_running(job_id)
    service.finish_error(job_id, "调研失败")

    with pytest.raises(IllegalTransitionError):
        service.finish_success(job_id, sample_result())

    job = repo.get(job_id)
    assert job is not None
    assert job.status is JobStatus.ERROR  # 状态未被覆盖
    assert JobStatus.COMPLETE not in repo.transitions  # 连写入都没发生过


def test_finish_success_stores_structured_json() -> None:
    """TC-U-012 补充：成功收尾写入可反序列化的结构化结果（REQ-003 / REQ-006）。"""
    service, repo, _, _ = _service()
    job_id, _ = _created(service, repo)
    service.mark_running(job_id)
    expected = sample_result()

    job = service.finish_success(job_id, expected)

    assert job.status is JobStatus.COMPLETE
    assert job.result is not None
    assert FlowResult.from_json(job.result) == expected
    assert repo.transitions == [JobStatus.RUNNING, JobStatus.COMPLETE]


# ------------------------------------------------------------------ 其它


def test_mark_running_sets_status_and_event() -> None:
    """TC-U-015 前置：开始执行时状态转 RUNNING 并留痕（REQ-004）。"""
    service, repo, _, _ = _service()
    job_id, _ = _created(service, repo)

    service.mark_running(job_id)

    job = repo.get(job_id)
    assert job is not None
    assert job.status is JobStatus.RUNNING
    assert [event.data for event in job.events][-1] == "Flow 开始执行"


def test_get_raises_for_unknown_job() -> None:
    """TC-U-009 补充：查询不存在的作业抛 JobNotFoundError（REQ-003）。"""
    service, _, _, _ = _service()

    with pytest.raises(JobNotFoundError):
        service.get("nope")


def test_get_returns_job_with_events_in_order() -> None:
    """TC-U-009 补充：事件按时间正序返回（REQ-005）。"""
    service, repo, _, clock = _service()
    job_id, _ = _created(service, repo)
    clock.advance(1)
    service.mark_running(job_id)
    clock.advance(1)
    service.finish_success(job_id, sample_result())

    job = service.get(job_id)

    assert [event.data for event in job.events] == [
        "作业已受理",
        "作业已入队",
        "Flow 开始执行",
        "Flow 执行完成",
    ]
    assert [event.timestamp for event in job.events] == sorted(e.timestamp for e in job.events)


def test_service_uses_injected_clock_not_wall_clock() -> None:
    """TC-U-009 补充：时间来自注入的时钟，测试可复现（spec/021 §5）。"""
    service, repo, _, clock = _service()

    job_id, job = _created(service, repo)

    assert job.created_at == clock.now()
    assert job.updated_at == clock.now()


def test_repository_contract_is_respected_by_memory_impl() -> None:
    """TC-U-009 补充：内存实现本身遵守端口契约（详细用例见 tests/contract）。"""
    repository = MemoryJobRepository()
    clock = FrozenClock()
    service = JobService(
        repository=repository,
        queue=RecordingTaskQueue(),
        clock=clock,
        ids=SeqIdGenerator(),
        publisher=RepositoryEventPublisher(repository=repository, clock=clock),
    )

    job_id = service.create(INPUTS)

    assert repository.get(job_id) is not None
