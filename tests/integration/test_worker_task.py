"""Worker 侧任务体的集成用例（spec/040 §2.3）。

用例编号：TC-I-013。用 Celery 的 `task_always_eager` 把「投递 → 任务体 → 执行器 →
落库」压进本进程：不需要 Redis，也不需要真实 crewai（执行器由测试注入，Runner 用
`FakeFlowRunner`）。

注意 `task_always_eager` 只影响 `apply_async`：`send_task` 在 eager 下**仍会连 broker**
（celery 自己会发 `AlwaysEagerIgnored` 警告），所以队列适配器必须按名字取到已注册的任务。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from celery import Celery

from amr.adapters.celery_queue import KICKOFF_TASK_NAME, CeleryTaskQueue
from amr.adapters.memory_repository import MemoryJobRepository
from amr.adapters.repository_publisher import RepositoryEventPublisher
from amr.adapters.system_clock import Uuid4Generator
from amr.domain.inputs import CrewInputs
from amr.domain.models import FlowResult
from amr.domain.status import JobStatus
from amr.services.job_service import JobService
from amr.services.orchestrator import FlowExecutor
from amr.worker import tasks as worker_tasks
from tests.fakes import FakeFlowRunner, RecordingTaskQueue
from tests.integration.conftest import TickingClock


@pytest.fixture
def eager_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[Celery]:
    """打开 eager 模式：`apply_async` 变成同进程同步执行，无需 broker。"""
    monkeypatch.setattr(worker_tasks.celery_app.conf, "task_always_eager", True)
    yield worker_tasks.celery_app


@pytest.fixture
def offline_worker(
    eager_mode: Celery, inputs: CrewInputs
) -> Iterator[tuple[CeleryTaskQueue, MemoryJobRepository, FakeFlowRunner, str]]:
    """装配一份「Worker 侧」：内存仓储 + 注入执行器，作业先以 PENDING 落库。"""
    repository = MemoryJobRepository()
    clock = TickingClock()
    publisher = RepositoryEventPublisher(repository=repository, clock=clock)
    service = JobService(
        repository=repository,
        queue=RecordingTaskQueue(),  # 提交阶段的队列：只记录，执行交给任务体
        clock=clock,
        ids=Uuid4Generator(),
        publisher=publisher,
    )
    runner = FakeFlowRunner()
    worker_tasks.set_executor(FlowExecutor(service=service, runner=runner, publisher=publisher))
    try:
        job_id = service.create(inputs)
        yield CeleryTaskQueue(eager_mode), repository, runner, job_id
    finally:
        worker_tasks.set_executor(None)  # 还原全局装配，避免污染其它用例


# ------------------------------------------------------------------ TC-I-013


def test_kickoff_flow_advances_job_to_terminal_state(
    offline_worker: tuple[CeleryTaskQueue, MemoryJobRepository, FakeFlowRunner, str],
    inputs: CrewInputs,
) -> None:
    """TC-I-013 eager 模式下任务体把作业推进到终态（REQ-002）。"""
    queue, repository, runner, job_id = offline_worker
    assert repository.get(job_id).status is JobStatus.PENDING  # type: ignore[union-attr]

    queue.enqueue_flow(job_id, inputs)

    job = repository.get(job_id)
    assert job is not None
    assert job.status is JobStatus.COMPLETE
    assert job.result is not None
    assert FlowResult.from_json(job.result) == runner.result
    assert runner.calls == [(job_id, inputs)]


def test_kickoff_flow_records_full_event_pipeline(
    offline_worker: tuple[CeleryTaskQueue, MemoryJobRepository, FakeFlowRunner, str],
    inputs: CrewInputs,
) -> None:
    """TC-I-013 补充：任务体执行后事件流水完整且正序（REQ-005）。"""
    queue, repository, _, job_id = offline_worker

    queue.enqueue_flow(job_id, inputs)

    job = repository.get(job_id)
    assert job is not None
    assert [event.data for event in job.events] == [
        "作业已受理",
        "作业已入队",
        "Flow 开始执行",
        "调研完成",
        "战略完成",
        "Flow 执行完成",
    ]
    assert [event.timestamp for event in job.events] == sorted(e.timestamp for e in job.events)


def test_task_is_registered_by_the_shared_name(eager_mode: Celery) -> None:
    """TC-I-013 补充：生产者与消费者共用同一个任务名，且 ack 策略是 late（REQ-002）。"""
    assert KICKOFF_TASK_NAME in eager_mode.tasks
    assert eager_mode.conf.task_acks_late is True, "Worker 崩溃后消息必须可重投（REQ-002）"
