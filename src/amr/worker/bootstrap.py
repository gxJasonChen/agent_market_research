"""Worker 侧的生产装配（spec/021 §7）。

全仓库只有两处允许出现「按配置选实现」的分叉：`api/app.py` 的工厂与这里。
业务代码（`services/`、`domain/`）里不得出现 `if 生产/测试`。

**函数内 import 是刻意的**：`crewai` 与 `mysql.connector` 加载很重，放在模块顶层会让
`import amr.worker.bootstrap` 这个动作本身变慢，而 API 进程恰好要 import 它
（复用 Celery app 声明）。延迟到真正装配那一刻，只影响要用到它的那次调用。
"""

from __future__ import annotations

import logging

from amr.adapters.repository_publisher import RepositoryEventPublisher
from amr.adapters.system_clock import SystemClock, Uuid4Generator
from amr.config import Settings
from amr.domain.errors import QueueUnavailableError
from amr.domain.inputs import CrewInputs
from amr.ports.repository import JobRepository
from amr.ports.runner import FlowRunner
from amr.services.job_service import JobService
from amr.services.orchestrator import FlowExecutor

__all__ = ["build_executor", "build_repository", "build_runner"]

logger = logging.getLogger(__name__)


def build_repository(settings: Settings) -> JobRepository:
    """生产仓储：`MySQLJobRepository`（WS-A 交付）。

    连接参数一律经由 `Settings` 传入，源码里不出现主机名与口令（spec/000 §7）；
    仓储自身在首次使用时才建表连库，所以这里不会在装配期触网（spec/010 §4.1）。
    """
    from amr.adapters.mysql_repository import MySQLJobRepository

    return MySQLJobRepository(settings)


def build_runner(settings: Settings) -> FlowRunner:
    """按配置选执行器。

    `fake` 走的是**离线确定性执行器**而不是「假模型」——CrewAI 的 `LLM` 会把 `call`
    委托给 provider 实现，桩对象拦不住（spec/021 §4.1 有实测记录）。这条路径覆盖
    HTTP → JobService → Celery → Worker → FlowExecutor → Repository 全链路，
    不覆盖 CrewAI 自身行为（那由真实网关的系统测试 TC-S-004 负责）。
    """
    if settings.llm_provider == "fake":
        from amr.adapters.fake_runner import FakeFlowRunner

        logger.warning("AMR_LLM_PROVIDER=fake：使用离线确定性执行器，不会发生真实模型调用")
        return FakeFlowRunner()

    from amr.adapters.crewai_runner import CrewAIFlowRunner
    from amr.adapters.llm_factory import build_llm_provider

    model = build_llm_provider(settings).build()
    return CrewAIFlowRunner(model)


def build_executor(settings: Settings) -> FlowExecutor:
    """Worker 进程的完整装配：仓储 → 事件落点 → 作业服务 → Runner → 执行器。"""
    repository = build_repository(settings)
    clock = SystemClock()
    publisher = RepositoryEventPublisher(repository=repository, clock=clock)
    service = JobService(
        repository=repository,
        queue=_NoEnqueue(),
        clock=clock,
        ids=Uuid4Generator(),
        publisher=publisher,
    )
    logger.info("Worker 已装配执行器")
    return FlowExecutor(service=service, runner=build_runner(settings), publisher=publisher)


class _NoEnqueue:
    """Worker 侧的 `JobService` 不负责入队（入队是 API 进程的事）。

    传一个「一用就报错」的对象，而不是 `None` 或一个看起来可用的队列：
    `JobService` 的签名要求 `TaskQueue`，静默的 `None` 只会在别处变成更难排查的
    `AttributeError`；而真给一个能用的队列，就等于允许 Worker 自己给自己派活。
    """

    def enqueue_flow(self, job_id: str, inputs: CrewInputs) -> None:
        """被调用到说明装配错了，必须响。"""
        raise QueueUnavailableError(
            "Worker 进程的作业服务不用于入队",
            context={"job_id": job_id, "customer_domain": inputs.customer_domain},
        )
