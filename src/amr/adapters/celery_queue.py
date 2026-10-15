"""`TaskQueue` 的 Celery 实现（spec/021 §2）。

两个刻意的技术选择：

1. **用 `apply_async` 而不是 `send_task`。** `send_task` 在 `task_always_eager=True` 下
   仍会去连 broker（celery 自己会发 `AlwaysEagerIgnored` 警告），集成层「Celery eager」
   这条要求（spec/040 §1）就落不了地。按名字取已注册的任务再 `apply_async`，
   eager 时同进程执行，非 eager 时正常投递。
2. **失败只记异常类型，不记原始消息。** kombu 的连接异常里可能带 broker 连接串与口令，
   而 spec/000 §7 禁止把连接串写进日志；异常链用 `raise ... from exc` 保留给排障者。
"""

from __future__ import annotations

import logging

from celery import Celery

from amr.domain.errors import QueueUnavailableError
from amr.domain.inputs import CrewInputs

__all__ = ["KICKOFF_TASK_NAME", "CeleryTaskQueue"]

logger = logging.getLogger(__name__)

#: 任务名是生产者与消费者的**共享契约**：`worker/tasks.py` 用它注册任务，这里用它投递。
#: 定义在生产者一侧，避免适配层反向依赖 worker 包。
KICKOFF_TASK_NAME = "amr.kickoff_flow"


class CeleryTaskQueue:
    """把作业投递给 Celery broker。"""

    def __init__(self, app: Celery, task_name: str = KICKOFF_TASK_NAME) -> None:
        """`app` 用 `worker.tasks.celery_app`：进程里只有一份 Celery 配置（spec/021 §7）。"""
        self._app = app
        self._task_name = task_name

    def enqueue_flow(self, job_id: str, inputs: CrewInputs) -> None:
        """投递一次 Flow 执行。

        消息体只有 `job_id` 与入参：作业状态一律以仓储为准，不靠消息传递，
        否则消息与库里的状态会各说各话（REQ-004）。

        投递失败（broker 不可达、任务未注册）抛 `QueueUnavailableError`，
        由 `JobService.create` 把作业置成 `ERROR`——不留僵尸 `PENDING`（REQ-011）。
        """
        try:
            task = self._app.tasks[self._task_name]
            task.apply_async(args=[job_id, inputs.model_dump(mode="json")])
        except KeyError as exc:
            # 任务没注册 = 没人会消费这条消息，等同于队列不可用
            logger.error("任务未注册，无法投递 task=%s job_id=%s", self._task_name, job_id)
            raise QueueUnavailableError(
                "任务未注册，无法投递",
                context={"job_id": job_id, "task": self._task_name},
            ) from exc
        except Exception as exc:  # noqa: BLE001 —— 投递不出去就是队列不可用，见模块 docstring
            logger.error(
                "投递作业失败 job_id=%s task=%s reason=%s",
                job_id,
                self._task_name,
                type(exc).__name__,
            )
            raise QueueUnavailableError(
                "任务队列不可用",
                context={"job_id": job_id, "reason": type(exc).__name__},
            ) from exc
