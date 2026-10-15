"""Worker 侧的执行编排（spec/021 §3.1）。

这个类很短，但它是源项目最严重缺陷的落户处，所以结构上刻意写成「成功路径在异常
处理之外」：异常分支收尾后**必须 `return`**，绝不允许往下走到 `finish_success`。
写成两个并列 `if/else` 是同一个 bug 的另一种写法，review 直接打回。
"""

from __future__ import annotations

import logging

from amr.domain.errors import IllegalTransitionError
from amr.domain.inputs import CrewInputs
from amr.ports.events import EventPublisher
from amr.ports.runner import FlowRunner
from amr.services.job_service import JobService

__all__ = ["FlowExecutor"]

logger = logging.getLogger(__name__)


class FlowExecutor:
    """把作业从「已入队」推进到「终态已落库」。"""

    def __init__(
        self,
        service: JobService,
        runner: FlowRunner,
        publisher: EventPublisher,
    ) -> None:
        self._service = service
        self._runner = runner
        self._publisher = publisher

    def execute(self, job_id: str, inputs: CrewInputs) -> None:
        """执行一次作业。

        `job_id` 已是终态时安静跳过：队列是 at-least-once 的，重复投递不该被当成错误，
        更不该把 Celery 拖进重试风暴。
        """
        try:
            self._service.mark_running(job_id)
        except IllegalTransitionError as exc:
            logger.warning("作业已处于终态，跳过重复投递 job_id=%s: %s", job_id, exc)
            return

        try:
            result = self._runner.run(job_id, inputs, self._publisher.sink(job_id))
        except Exception as exc:  # noqa: BLE001 —— 任何下游异常都必须把作业落成 ERROR
            logger.exception("Flow 执行失败 job_id=%s", job_id)
            self._service.finish_error(job_id, exc)
            return  # ← 关键：收尾之后立刻返回，成功分支不允许再被执行

        self._service.finish_success(job_id, result)
