"""Celery 应用与任务声明（spec/021 §7）。

任务体只做三件事：还原 `CrewInputs` → 取执行器 → `execute`。**状态机逻辑一律不在这里**
（spec/010 §3）：它属于 `services/orchestrator.py`，那里已经写成「异常分支收尾后立刻
返回」的形状——源项目最严重的缺陷就是从这里长出来的（REQ-004）。

`celery_app` 是本系统 Celery 配置的唯一声明处：API 进程借它投递，Worker 进程借它消费。
"""

from __future__ import annotations

import logging
from typing import Any

from celery import Celery

from amr.adapters.celery_queue import KICKOFF_TASK_NAME
from amr.config import Settings, configure_logging, export_third_party_env, get_settings
from amr.domain.errors import RepositoryUnavailableError
from amr.domain.inputs import CrewInputs
from amr.ports.runner import FlowExecutor
from amr.worker import bootstrap

__all__ = ["celery_app", "get_executor", "kickoff_flow", "set_executor"]

logger = logging.getLogger(__name__)

settings: Settings = get_settings()
configure_logging(settings)
export_third_party_env(settings)

celery_app = Celery("amr", broker=settings.redis_broker_url)
celery_app.conf.update(
    # Worker 崩在任务中途时消息必须能重投，否则作业会永远停在 PENDING/RUNNING（REQ-002）。
    # 重投是安全的：执行器用占用语义（expected_from=PENDING）保证同一作业只被真正跑一次。
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # 一个 Worker 预取一条：Flow 是长任务，抢一堆消息只会让其它 Worker 空转
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    # ---- 长任务的 at-least-once 边界（REQ-002）----
    # Redis transport 默认 visibility_timeout=3600s：**超过 1 小时的 Flow 会被判定为
    # 「消息丢了」而重投**。虽然占用语义能挡住重复执行，但会让作业卡在 RUNNING
    # （重投那次拿不到占用就跳过了，而真正在跑的那次已经没人记得）。这里放到 6 小时。
    broker_transport_options={"visibility_timeout": 6 * 60 * 60},
    # 硬/软时限：Flow 挂死时至少能把 Worker 的槽位收回来。软限时抛异常、硬限时杀进程，
    # 两者都留出足够余量——真实推理要跑几分钟到几十分钟。
    task_soft_time_limit=45 * 60,
    task_time_limit=50 * 60,
    # 投递失败/worker 崩溃这类**暂时性**故障由 kickoff_flow 自己有限重试（见下），
    # 这里不开启全局 autoretry：整条 Flow 重跑的代价远高于重试一次写入。
    task_acks_on_failure_or_timeout=True,
)

#: 进程级执行器。首次用到时才装配（延迟 import 见 worker/bootstrap.py）
_executor: FlowExecutor | None = None


def set_executor(executor: FlowExecutor | None) -> None:
    """注入执行器；传 `None` 则退回「按 Settings 现装」的行为。

    给测试与运维留的注入点：集成测试用 Celery eager 模式跑任务体时，注入一个
    内存版的执行器即可完全离线（spec/040 §2.3 TC-I-013）。
    """
    global _executor
    _executor = executor


def get_executor() -> FlowExecutor:
    """取进程级执行器，第一次调用时按 `Settings` 装配。"""
    global _executor
    if _executor is None:
        _executor = bootstrap.build_executor(get_settings())
    return _executor


@celery_app.task(name=KICKOFF_TASK_NAME, bind=True, max_retries=3, default_retry_delay=30)
def kickoff_flow(self: Any, job_id: str, inputs: dict[str, Any]) -> None:
    """消费一条投递：把作业推进到终态。

    消息里只有 `job_id` 与入参；作业状态以仓储为准（REQ-004）。入参在这里再过一次
    `CrewInputs` 校验：Worker 收到的消息同样属于**不可信输入**（spec/000 §7）。

    重试策略是**有限且有选择性**的（REQ-002）：

    - 只在存储**暂时**不可用时重试，且最多 3 次。这类失败常见于「Worker 抢到任务时
      数据库刚好在重启」，重试一次就恢复了；
    - 不重试整条 Flow 失败——那要再烧一遍 token，代价与重试一次拿连接不成比例；
    - 有限次是必须的：无上限重试会把一条坏消息变成刷屏的毒丸。
    """
    logger.info("开始处理作业 job_id=%s", job_id)
    try:
        get_executor().execute(job_id, CrewInputs.model_validate(inputs))
    except RepositoryUnavailableError as exc:
        logger.warning(
            "存储暂时不可用，重试作业 job_id=%s（第 %s 次）", job_id, self.request.retries + 1
        )
        raise self.retry(exc=exc) from exc
