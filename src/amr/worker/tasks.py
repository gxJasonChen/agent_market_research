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
from amr.config import Settings, get_settings
from amr.domain.inputs import CrewInputs
from amr.ports.runner import FlowExecutor
from amr.worker import bootstrap

__all__ = ["celery_app", "get_executor", "kickoff_flow", "set_executor"]

logger = logging.getLogger(__name__)

settings: Settings = get_settings()

celery_app = Celery("amr", broker=settings.redis_broker_url)
celery_app.conf.update(
    # Worker 崩在任务中途时消息必须能重投，否则作业会永远停在 PENDING/RUNNING（REQ-002）。
    # 重投是安全的：执行器对已进入终态的作业会安静跳过。
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # 一个 Worker 预取一条：Flow 是长任务，抢一堆消息只会让其它 Worker 空转
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
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


@celery_app.task(name=KICKOFF_TASK_NAME)
def kickoff_flow(job_id: str, inputs: dict[str, Any]) -> None:
    """消费一条投递：把作业推进到终态。

    消息里只有 `job_id` 与入参；作业状态以仓储为准（REQ-004）。入参在这里再过一次
    `CrewInputs` 校验：Worker 收到的消息同样属于**不可信输入**（spec/000 §7）。
    """
    logger.info("开始处理作业 job_id=%s", job_id)
    get_executor().execute(job_id, CrewInputs.model_validate(inputs))
