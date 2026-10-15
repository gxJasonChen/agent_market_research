"""集成测试的前置设施（spec/040 §2.3）。

整层**离线可跑**：内存仓储 + 内存队列 + `FakeFlowRunner`，不需要 MySQL / Redis /
模型网关（NFR-001）。装配路径与生产完全一致——`create_app` 把全部依赖作为参数注入
（spec/021 §7），这里只是把实现换成内存版，因此跑通的代码路径就是生产路径。

事件顺序断言依赖**注入时钟**：每次 `now()` 前进 1 秒，时间戳严格递增（spec/040 §5
禁止依赖 wall clock 与 `sleep`）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from amr.adapters.inline_queue import InlineTaskQueue
from amr.adapters.memory_repository import MemoryJobRepository
from amr.adapters.repository_publisher import RepositoryEventPublisher
from amr.adapters.system_clock import Uuid4Generator
from amr.api.app import create_app
from amr.config import Settings
from amr.domain.inputs import CrewInputs
from amr.domain.models import Job
from amr.domain.status import JobStatus
from amr.ports.clock import Clock, IdGenerator
from amr.ports.queue import TaskQueue
from amr.ports.repository import JobRepository
from amr.ports.runner import FlowRunner
from amr.services.job_service import JobService
from amr.services.orchestrator import FlowExecutor
from tests.fakes import FakeFlowRunner

__all__ = ["ExplodingRepository", "Harness", "TickingClock"]

#: 契约样例入参（spec/022 CT-001）
PAYLOAD: dict[str, str] = {
    "customer_domain": "openai.com",
    "project_description": "为一家 AI 基础设施公司制定新品发布营销方案",
}

STARTS_AT = datetime(2026, 10, 15, 10, 0, 0, tzinfo=UTC)


class TickingClock:
    """每读一次时间就走 1 秒的确定性时钟（spec/021 §5）。

    真实时钟在毫秒级任务里会给出**相同**的时间戳，那样「事件按时间正序」就成了
    一句无法证伪的断言；注入时钟把先后关系固化下来。
    """

    STEP = timedelta(seconds=1)

    def __init__(self, start: datetime = STARTS_AT) -> None:
        self._now = start

    def now(self) -> datetime:
        current = self._now
        self._now = current + self.STEP
        return current


class _ExecutorHandle:
    """`InlineTaskQueue → FlowExecutor → JobService → InlineTaskQueue` 是个环。

    队列需要执行器、执行器需要（持有队列的）服务，所以用这个句柄做一次**晚期绑定**，
    比让两个 fixture 互相依赖更直白。
    """

    def __init__(self) -> None:
        self._executor: FlowExecutor | None = None

    def bind(self, executor: FlowExecutor) -> None:
        self._executor = executor

    def __call__(self, job_id: str, inputs: CrewInputs) -> None:
        if self._executor is None:  # pragma: no cover - 装配错误
            raise RuntimeError("执行器尚未绑定，装配顺序有误")
        self._executor.execute(job_id, inputs)


class ExplodingRepository:
    """任何调用都抛指定异常的仓储替身。

    两个用途：存储不可用 → 503（TC-I-007）；未预期异常 → 500 且不泄露内部信息
    （TC-I-012）。异常**实例由调用方提供**，因此测试可以把「危险文案」放进去，
    验证它不会漏到响应体里。
    """

    def __init__(self, error: BaseException) -> None:
        self.error = error

    def create(self, job: Job) -> None:
        raise self.error

    def get(self, job_id: str) -> Job | None:
        raise self.error

    def append_event(self, job_id: str, data: str, *, at: datetime) -> None:
        raise self.error

    def transition(
        self,
        job_id: str,
        to: JobStatus,
        *,
        result: str | None = None,
        at: datetime,
        expected_from: JobStatus | None = None,
    ) -> Job:
        raise self.error

    def list_recent(self, limit: int = 20) -> list[Job]:
        raise self.error


@dataclass(slots=True)
class Harness:
    """一次装配出来的「离线系统」：HTTP 客户端 + 各注入件，供断言直接取用。"""

    client: TestClient
    app: FastAPI
    repository: JobRepository
    queue: TaskQueue
    clock: Clock
    runner: FlowRunner
    executor: FlowExecutor


MakeApp = Callable[..., Harness]


@pytest.fixture
def make_app() -> Iterator[MakeApp]:
    """按需装配一个应用，默认使用全套内存实现。

    默认队列是 `InlineTaskQueue`（同进程同步执行），因此「提交一次作业」在测试里
    就等于跑完「提交 → 执行 → 落库」整条链路（spec/021 §2）。
    """
    created: list[TestClient] = []

    def _make(
        *,
        runner: FlowRunner | None = None,
        queue: TaskQueue | None = None,
        repository: JobRepository | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        settings: Settings | None = None,
        raise_server_exceptions: bool = True,
    ) -> Harness:
        resolved_repository = repository if repository is not None else MemoryJobRepository()
        resolved_clock = clock if clock is not None else TickingClock()
        resolved_ids = ids if ids is not None else Uuid4Generator()
        resolved_runner = runner if runner is not None else FakeFlowRunner()
        # 不读仓库根的 .env：集成测试必须与环境无关（spec/040 §5）
        resolved_settings = settings if settings is not None else Settings(_env_file=None)

        publisher = RepositoryEventPublisher(repository=resolved_repository, clock=resolved_clock)
        handle = _ExecutorHandle()
        resolved_queue = queue if queue is not None else InlineTaskQueue(handle)
        service = JobService(
            repository=resolved_repository,
            queue=resolved_queue,
            clock=resolved_clock,
            ids=resolved_ids,
            publisher=publisher,
        )
        executor = FlowExecutor(service=service, runner=resolved_runner, publisher=publisher)
        handle.bind(executor)

        app = create_app(
            resolved_settings,
            repository=resolved_repository,
            queue=resolved_queue,
            clock=resolved_clock,
            ids=resolved_ids,
        )
        client = TestClient(app, raise_server_exceptions=raise_server_exceptions)
        created.append(client)
        return Harness(
            client=client,
            app=app,
            repository=resolved_repository,
            queue=resolved_queue,
            clock=resolved_clock,
            runner=resolved_runner,
            executor=executor,
        )

    yield _make

    for client in created:
        client.close()


@pytest.fixture
def payload() -> dict[str, str]:
    """契约样例请求体（spec/022 CT-001）。"""
    return dict(PAYLOAD)


@pytest.fixture
def inputs() -> CrewInputs:
    """样例请求体对应的领域对象——Worker/执行器侧拿到的东西。"""
    return CrewInputs(**PAYLOAD)
