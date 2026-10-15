"""HTTP 进程的依赖装配（spec/021 §7）。

`create_app` 必须支持**全部依赖作为参数注入**——集成测试才能在零外部依赖下跑通整层
（NFR-001）。无参调用时按 `Settings` 装配生产实现，且**推迟到首个请求**：
`import amr.api.app` 会被 `uvicorn amr.api.app:app` 在进程启动时执行，那一刻不该连
MySQL、也不该 import celery/crewai。源项目的病根正是「import 期就连数据库建表」
（spec/010 §4.1）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import cast

from fastapi import FastAPI, Request

from amr.adapters.repository_publisher import RepositoryEventPublisher
from amr.adapters.system_clock import SystemClock, Uuid4Generator
from amr.config import Settings
from amr.ports.clock import Clock, IdGenerator
from amr.ports.queue import TaskQueue
from amr.ports.repository import JobRepository
from amr.services.job_service import JobService

__all__ = [
    "Dependencies",
    "build_dependencies",
    "get_dependencies",
    "get_job_service",
    "install_dependencies",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Dependencies:
    """一次装配的产物。

    路由只认识 `service`；其余字段留给排障、扩展开关与测试断言。
    """

    settings: Settings
    repository: JobRepository
    queue: TaskQueue
    clock: Clock
    ids: IdGenerator
    service: JobService


def build_dependencies(
    settings: Settings,
    *,
    repository: JobRepository | None = None,
    queue: TaskQueue | None = None,
    clock: Clock | None = None,
    ids: IdGenerator | None = None,
) -> Dependencies:
    """装配一份依赖：**注入的优先，缺的用生产实现**。

    这是全仓库仅有的两个「生产 / 测试」分叉点之一（另一个是 `worker/bootstrap.py`），
    业务代码里不得出现这类分支（spec/021 §7）。
    """
    resolved_clock: Clock = clock if clock is not None else SystemClock()
    resolved_ids: IdGenerator = ids if ids is not None else Uuid4Generator()
    resolved_repository: JobRepository = (
        repository if repository is not None else _production_repository(settings)
    )
    resolved_queue: TaskQueue = queue if queue is not None else _production_queue(settings)
    # 事件落点与作业服务是绑死的：REQ-005 要求每次状态变更都留一条流水，因此 publisher
    # 不是可选参数——做成可选只会让「事件被静默丢弃」成为一种常态
    publisher = RepositoryEventPublisher(repository=resolved_repository, clock=resolved_clock)
    service = JobService(
        repository=resolved_repository,
        queue=resolved_queue,
        clock=resolved_clock,
        ids=resolved_ids,
        publisher=publisher,
    )
    return Dependencies(
        settings=settings,
        repository=resolved_repository,
        queue=resolved_queue,
        clock=resolved_clock,
        ids=resolved_ids,
        service=service,
    )


def _production_repository(settings: Settings) -> JobRepository:
    """生产仓储：复用 Worker 侧的生产装配，避免两份「MySQL 怎么连」的知识。"""
    from amr.worker.bootstrap import build_repository

    return build_repository(settings)


def _production_queue(settings: Settings) -> TaskQueue:
    """生产队列：`worker/tasks.py` 是 Celery app 的唯一声明处，API 进程只当生产者。"""
    from amr.adapters.celery_queue import CeleryTaskQueue
    from amr.worker.tasks import celery_app

    # 只回显 host:port/db，**不带口令**：spec/000 §7 禁止把连接串写进日志
    logger.info(
        "已装配生产任务队列 broker=%s:%s/%s",
        settings.redis_host,
        settings.redis_port,
        settings.redis_db,
    )
    return CeleryTaskQueue(celery_app)


class _LazyDependencies:
    """把装配推迟到首次使用，并且只做一次。

    用锁而不是「import 时就算好」：`create_app()` 在模块 import 期被 uvicorn 调用，
    那时既不该连 MySQL，也不该加载 celery（NFR-001：本地起服务、打 `/healthz`、
    跑集成测试都不依赖外部系统）。
    """

    def __init__(self, factory: Callable[[], Dependencies]) -> None:
        self._factory = factory
        self._dependencies: Dependencies | None = None
        self._lock = Lock()

    def get(self) -> Dependencies:
        with self._lock:
            if self._dependencies is None:
                self._dependencies = self._factory()
            return self._dependencies


def install_dependencies(app: FastAPI, factory: Callable[[], Dependencies]) -> None:
    """把装配工厂挂到 `app.state` 上，供依赖函数取用。"""
    app.state.dependencies = _LazyDependencies(factory)


def get_dependencies(request: Request) -> Dependencies:
    """取出本次进程的装配结果（首次调用时才会真正装配）。"""
    return cast(_LazyDependencies, request.app.state.dependencies).get()


def get_job_service(request: Request) -> JobService:
    """路由的依赖：作业服务。"""
    return get_dependencies(request).service
