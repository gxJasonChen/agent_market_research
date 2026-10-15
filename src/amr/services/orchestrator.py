"""Worker 侧的执行编排（spec/021 §3.1）。

这个类很短，但它是源项目最严重缺陷的落户处，所以结构上刻意写成「成功路径在异常
处理之外」：异常分支收尾后**必须 `return`**，绝不允许往下走到 `finish_success`。
写成两个并列 `if/else` 是同一个 bug 的另一种写法，review 直接打回。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from functools import partial

from amr.domain.errors import IllegalTransitionError, RepositoryUnavailableError
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
        *,
        finalize_attempts: int = 3,
        finalize_backoff_seconds: float = 0.5,
    ) -> None:
        """`finalize_attempts` 是**收尾写库**的重试次数，不是整条 Flow 的重试次数。

        Flow 已经跑完、结果就在手里时，重试「把结果写进库」是廉价且有效的；
        重试整条 Flow 则是双倍 token。两者必须分开——见 `_finalize`。
        """
        self._service = service
        self._runner = runner
        self._publisher = publisher
        self._finalize_attempts = max(1, finalize_attempts)
        self._finalize_backoff_seconds = finalize_backoff_seconds

    def execute(self, job_id: str, inputs: CrewInputs) -> None:
        """执行一次作业。

        拿不到「占用」就安静跳过：队列是 at-least-once 的，重复投递不该被当成错误，
        更不该把 Celery 拖进重试风暴。两种情况都属于「本轮不该由我执行」：

        - 作业已是终态（别人收尾了，或重复投递到已完成的作业上）；
        - 作业已在 `RUNNING`（另一个 Worker 正拿着它，或上一轮跑完但收尾没落库）。

        第二种情况以前是漏的：`RUNNING → RUNNING` 在领域层是合法的幂等写入，
        于是重复投递会让**整条 Flow 再跑一遍**。现在由 `mark_running` 的占用语义拦住
        （`expected_from=PENDING`，见 `JobService.mark_running`）。
        """
        try:
            self._service.mark_running(job_id)
        except IllegalTransitionError as exc:
            logger.warning("作业已进入终态或已被占用，跳过本次投递 job_id=%s: %s", job_id, exc)
            return

        try:
            result = self._runner.run(job_id, inputs, self._publisher.sink(job_id))
        except Exception as exc:  # noqa: BLE001 —— 任何下游异常都必须把作业落成 ERROR
            logger.exception("Flow 执行失败 job_id=%s", job_id)
            # 用 partial **立即绑定**异常对象：`except ... as exc` 的名字在块结束时就解绑了，
            # 靠闭包引用会变成「游离变量」错误（这里踩过一次）。partial 也比 lambda 更好过类型检查。
            self._finalize(partial(self._service.finish_error, job_id, exc), job_id, "ERROR")
            return  # ← 关键：收尾之后立刻返回，成功分支不允许再被执行

        self._finalize(lambda: self._service.finish_success(job_id, result), job_id, "COMPLETE")

    def _finalize(self, write: Callable[[], object], job_id: str, target: str) -> None:
        """写终态，对**暂时性存储故障**做有限重试。

        为什么需要它：收尾这一刻数据库恰好不可用，作业就会永久停在 `RUNNING`——
        而 `PENDING` 侧有入队失败的防护，`RUNNING` 侧原来什么都没有。

        为什么只重试「写」不重试「跑」：Flow 结果已经在手里，重试写入是毫秒级动作；
        重跑整条 Flow 要再烧一遍 token。两者的代价差几个数量级。

        重试耗尽仍然失败就**抛出去**：Celery 会把任务标记为失败，作业留给
        `scripts/reap_stuck_jobs.py` 回收（`docs/RUNBOOK.md` 的「卡住的作业」一节）。
        绝不吞掉——吞掉等于把「作业卡住且无人知晓」变成常态。
        """
        last: RepositoryUnavailableError | None = None
        for attempt in range(1, self._finalize_attempts + 1):
            try:
                write()
                return
            except RepositoryUnavailableError as exc:
                last = exc
                logger.warning(
                    "写入终态 %s 失败（第 %d/%d 次）job_id=%s",
                    target,
                    attempt,
                    self._finalize_attempts,
                    job_id,
                )
                if attempt < self._finalize_attempts:
                    time.sleep(self._finalize_backoff_seconds * attempt)
        assert last is not None  # 循环只有在抛过异常时才可能走到这里
        raise last
