"""`FlowExecutor` 与 `RepositoryEventPublisher` 单元测试。

守护需求：REQ-004（失败优先）、REQ-005（事件流水）、REQ-006（Schema 校验）。
用例编号：TC-U-013 ~ TC-U-016。
"""

from __future__ import annotations

from datetime import datetime

from amr.adapters.repository_publisher import RepositoryEventPublisher
from amr.domain.errors import AmrError, IllegalTransitionError, RepositoryUnavailableError
from amr.domain.inputs import CrewInputs
from amr.domain.models import FlowResult
from amr.domain.status import JobStatus
from amr.services.job_service import JobService
from amr.services.orchestrator import FlowExecutor
from tests.fakes import (
    FAILING_MESSAGE,
    FailingFlowRunner,
    FakeFlowRunner,
    FrozenClock,
    RecordingJobRepository,
    RecordingTaskQueue,
    SchemaBrokenFlowRunner,
    SeqIdGenerator,
    sample_result,
)

INPUTS = CrewInputs(
    customer_domain="openai.com",
    project_description="为一家 AI 基础设施公司制定新品发布营销方案",
)


class _ExplodingRepository(RecordingJobRepository):
    """`append_event` 必炸的仓储，用于验证「可观测性故障不该拖垮业务」。"""

    def append_event(self, job_id: str, data: str, *, at: datetime) -> None:
        raise RepositoryUnavailableError("事件表写入失败")


def _executor(
    runner: FakeFlowRunner,
    *,
    repository: RecordingJobRepository | None = None,
) -> tuple[FlowExecutor, JobService, RecordingJobRepository]:
    repo = repository or RecordingJobRepository()
    clock = FrozenClock()
    publisher = RepositoryEventPublisher(repository=repo, clock=clock)
    service = JobService(
        repository=repo,
        queue=RecordingTaskQueue(),
        clock=clock,
        ids=SeqIdGenerator(),
        publisher=publisher,
    )
    return FlowExecutor(service=service, runner=runner, publisher=publisher), service, repo


def _enqueued(service: JobService) -> tuple[str, CrewInputs]:
    return service.create(INPUTS), INPUTS


# ------------------------------------------------------------------ TC-U-014 / 015


def test_execute_success_marks_complete_with_structured_result() -> None:
    """TC-U-014 成功路径：状态 COMPLETE，result 可反序列化（REQ-003 / REQ-006）。"""
    executor, service, repo = _executor(FakeFlowRunner())
    job_id, inputs = _enqueued(service)

    executor.execute(job_id, inputs)

    job = repo.get(job_id)
    assert job is not None
    assert job.status is JobStatus.COMPLETE
    assert job.result is not None
    assert FlowResult.from_json(job.result) == sample_result()


def test_execute_transition_sequence_is_running_then_complete() -> None:
    """TC-U-015 写入序列必须是 RUNNING → COMPLETE，不能跳过 RUNNING（REQ-004）。"""
    executor, service, repo = _executor(FakeFlowRunner())
    job_id, inputs = _enqueued(service)

    executor.execute(job_id, inputs)

    assert repo.transitions == [JobStatus.RUNNING, JobStatus.COMPLETE]


def test_execute_forwards_runner_events_to_repository() -> None:
    """TC-U-015 补充：Runner 发出的事件落进作业流水（REQ-005）。"""
    executor, service, repo = _executor(FakeFlowRunner(emit=["调研完成", "战略完成"]))
    job_id, inputs = _enqueued(service)

    executor.execute(job_id, inputs)

    job = repo.get(job_id)
    assert job is not None
    assert [event.data for event in job.events] == [
        "作业已受理",
        "作业已入队",
        "Flow 开始执行",
        "调研完成",
        "战略完成",
        "Flow 执行完成",
    ]


# ------------------------------------------------------------------ TC-U-013


def test_execute_failure_never_writes_complete() -> None:
    """TC-U-013 **异常路径之后从未出现 COMPLETE 写入**（REQ-004）。

    这是源项目「失败作业被误报成功」缺陷的核心回归测试：
    只看最终状态不够，必须断言成功分支**根本没被执行过**。
    """
    executor, service, repo = _executor(FailingFlowRunner())
    job_id, inputs = _enqueued(service)

    executor.execute(job_id, inputs)  # 不抛异常：失败在 Worker 内被收尾

    job = repo.get(job_id)
    assert job is not None
    assert job.status is JobStatus.ERROR
    assert job.result == FAILING_MESSAGE
    assert JobStatus.COMPLETE not in repo.transitions
    assert repo.transitions == [JobStatus.RUNNING, JobStatus.ERROR]


def test_execute_failure_result_is_stable_across_repeated_queries() -> None:
    """TC-U-013 补充：失败作业反复查询结果稳定为 ERROR（REQ-004）。"""
    executor, service, repo = _executor(FailingFlowRunner())
    job_id, inputs = _enqueued(service)

    executor.execute(job_id, inputs)

    for _ in range(3):
        job = repo.get(job_id)
        assert job is not None
        assert job.status is JobStatus.ERROR


def test_execute_wraps_schema_violation_as_error() -> None:
    """TC-U-013 补充：模型输出不合 Schema → 作业 ERROR，不把裸文本当结果（REQ-006）。"""
    executor, service, repo = _executor(SchemaBrokenFlowRunner())
    job_id, inputs = _enqueued(service)

    executor.execute(job_id, inputs)

    job = repo.get(job_id)
    assert job is not None
    assert job.status is JobStatus.ERROR
    assert job.result is not None and "Schema" in job.result


def test_execute_skips_duplicate_delivery_of_terminal_job() -> None:
    """TC-U-013 补充：重复投递已终态的作业时安静跳过，不重复执行（REQ-002）。

    at-least-once 语义下队列可能重复投递；此时应记日志后返回，
    而不是抛异常把 Celery 拖进重试风暴。
    """
    runner = FakeFlowRunner()
    executor, service, repo = _executor(runner)
    job_id, inputs = _enqueued(service)
    executor.execute(job_id, inputs)

    executor.execute(job_id, inputs)  # 第二次投递

    assert len(runner.calls) == 1  # Runner 没有被再跑一次
    assert repo.transitions == [JobStatus.RUNNING, JobStatus.COMPLETE]


# ------------------------------------------------------------------ TC-U-016


def test_publisher_swallows_repository_failure() -> None:
    """TC-U-016 事件写入失败不得中断业务，只记日志（REQ-005）。"""
    repo = _ExplodingRepository()
    publisher = RepositoryEventPublisher(repository=repo, clock=FrozenClock())

    publisher.publish("job-1", "随便什么事件")  # 不抛异常即通过

    assert publisher.sink("job-1") is not None


def test_execute_still_succeeds_when_event_writes_fail() -> None:
    """TC-U-016 补充：事件表坏掉时作业仍应正常完成（REQ-005）。"""
    executor, service, repo = _executor(FakeFlowRunner(), repository=_ExplodingRepository())
    job_id = service.create(INPUTS)  # create 里的事件写入同样会被吞掉

    executor.execute(job_id, INPUTS)

    job = repo.get(job_id)
    assert job is not None
    assert job.status is JobStatus.COMPLETE
    assert repo.transitions == [JobStatus.RUNNING, JobStatus.COMPLETE]


def test_publisher_sink_is_bound_to_job() -> None:
    """TC-U-016 补充：`sink(job_id)` 产出的事件写进对应作业（REQ-005）。"""
    repo = RecordingJobRepository()
    clock = FrozenClock()
    publisher = RepositoryEventPublisher(repository=repo, clock=clock)
    service = JobService(
        repository=repo,
        queue=RecordingTaskQueue(),
        clock=clock,
        ids=SeqIdGenerator(),
        publisher=publisher,
    )
    job_id = service.create(INPUTS)

    publisher.sink(job_id).emit("来自 Runner 的事件")

    job = repo.get(job_id)
    assert job is not None
    assert job.events[-1].data == "来自 Runner 的事件"


def test_illegal_transition_is_amr_error_subclass() -> None:
    """TC-U-016 补充：非法迁移异常属于本系统异常体系，HTTP 层可统一映射（REQ-011）。"""
    assert issubclass(IllegalTransitionError, AmrError)
