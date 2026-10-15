"""作业状态机（spec/021 §3.1）。

**本类是唯一有权改作业状态的地方。** 路由、Worker、Flow 都不直接写状态，
这样「什么时候能写、写成什么」只有一条代码路径，源项目那种「两个分支各写各的、
互相不知道对方」的情形在结构上就不可能发生（REQ-004）。
"""

from __future__ import annotations

import logging

from amr.domain.errors import AmrError, JobNotFoundError, QueueUnavailableError
from amr.domain.inputs import CrewInputs
from amr.domain.models import FlowResult, Job
from amr.domain.status import JobStatus
from amr.ports.clock import Clock, IdGenerator
from amr.ports.events import EventPublisher
from amr.ports.queue import TaskQueue
from amr.ports.repository import JobRepository

__all__ = ["JobService"]

logger = logging.getLogger(__name__)

EVENT_ACCEPTED = "作业已受理"
EVENT_ENQUEUED = "作业已入队"
EVENT_RUNNING = "Flow 开始执行"
EVENT_COMPLETED = "Flow 执行完成"
EVENT_ENQUEUE_FAILED = "入队失败：任务队列不可用"
ENQUEUE_FAILURE_RESULT = "任务队列不可用，作业未能入队"


class JobService:
    """作业的创建、查询与状态迁移。"""

    def __init__(
        self,
        repository: JobRepository,
        queue: TaskQueue,
        clock: Clock,
        ids: IdGenerator,
        publisher: EventPublisher,
    ) -> None:
        self._repository = repository
        self._queue = queue
        self._clock = clock
        self._ids = ids
        self._publisher = publisher

    # ------------------------------------------------------------ 创建

    def create(self, inputs: CrewInputs) -> str:
        """受理作业：落库 → 入队，返回 `job_id`（REQ-001）。

        顺序不可颠倒：先落库再入队。反过来的话，Worker 可能在作业还没写进数据库时
        就把它取走并开始执行，那条事件流水就会指向一个不存在的作业。
        """
        job_id = self._ids.new_id()
        now = self._clock.now()
        job = Job(
            job_id=job_id,
            status=JobStatus.PENDING,
            customer_domain=inputs.customer_domain,
            project_description=inputs.project_description,
            result=None,
            created_at=now,
            updated_at=now,
        )
        self._repository.create(job)
        self._emit(job_id, EVENT_ACCEPTED)

        try:
            self._queue.enqueue_flow(job_id, inputs)
        except QueueUnavailableError as exc:
            # 投递失败必须留下 ERROR —— 否则就是一个永远停在 PENDING 的僵尸作业（REQ-011）
            logger.error("作业入队失败 job_id=%s: %s", job_id, exc)
            self._repository.transition(
                job_id,
                JobStatus.ERROR,
                result=ENQUEUE_FAILURE_RESULT,
                at=self._clock.now(),
            )
            self._emit(job_id, EVENT_ENQUEUE_FAILED)
            raise

        self._emit(job_id, EVENT_ENQUEUED)
        return job_id

    # ------------------------------------------------------------ 查询

    def get(self, job_id: str) -> Job:
        """按 ID 取作业，不存在抛 `JobNotFoundError`（HTTP 层映射 404）。"""
        job = self._repository.get(job_id)
        if job is None:
            raise JobNotFoundError("作业不存在", context={"job_id": job_id})
        return job

    def list_recent(self, limit: int = 20) -> list[Job]:
        """最近的作业，按创建时间倒序。"""
        return self._repository.list_recent(limit)

    # ------------------------------------------------------------ 状态迁移

    def mark_running(self, job_id: str) -> Job:
        """`PENDING → RUNNING`，带走**占用语义**（REQ-002）。

        `expected_from=PENDING` 是关键：队列是 at-least-once 的，Worker 被 kill / 与 broker
        掉线后消息会被重投。没有这个约束的话，第二次投递会把作业从 `RUNNING` 再"迁到"
        `RUNNING`（domain 层把这个当幂等，是真值表允许的），于是**整条 Flow 再跑一遍**——
        双倍 token、两套 Agent、事件交错写进同一个作业。

        作业已在终态或已被别的 Worker 占用时抛 `IllegalTransitionError`，
        由执行器安静跳过（见 spec/021 §3.1）。
        """
        job = self._repository.transition(
            job_id,
            JobStatus.RUNNING,
            at=self._clock.now(),
            expected_from=JobStatus.PENDING,
        )
        self._emit(job_id, EVENT_RUNNING)
        return job

    def finish_success(self, job_id: str, result: FlowResult) -> Job:
        """`RUNNING → COMPLETE`，写入结构化结果（REQ-006）。

        作业已是 `ERROR` 时这里会抛 `IllegalTransitionError` —— 那是**期望行为**：
        失败作业不允许被改写成成功（REQ-004）。
        """
        job = self._repository.transition(
            job_id,
            JobStatus.COMPLETE,
            result=result.to_json(),
            at=self._clock.now(),
        )
        self._emit(job_id, EVENT_COMPLETED)
        return job

    def finish_error(self, job_id: str, error: BaseException | str) -> Job:
        """`* → ERROR`，把**经过筛选**的错误摘要写进 `result`（REQ-004 / CT-005）。

        摘要不是 `str(error)`——`result` 会原样回给调用方，而下游异常的原文可能带
        网关 URL、请求参数甚至密钥。规则是：

        - 本系统自己的异常（`AmrError`）：用它**由我们写的** `message`，
          外加白名单字段（如 Schema 校验失败的字段名）；
        - 其它异常：只给**类型名**，原文只进日志。

        这样调用方拿到的信息足够定位「哪一类失败」，又不至于把内部细节泄出去。
        """
        summary = _safe_summary(error)
        job = self._repository.transition(
            job_id,
            JobStatus.ERROR,
            result=summary,
            at=self._clock.now(),
        )
        self._emit(job_id, f"Flow 执行失败：{summary}")
        return job

    # ------------------------------------------------------------ 内部

    def _emit(self, job_id: str, data: str) -> None:
        # publisher 是必填依赖：REQ-005 要求每次状态变更都有事件，
        # 做成可选参数只会让「事件被静默丢弃」变成一种可能性
        self._publisher.publish(job_id, data)


def _safe_summary(error: BaseException | str) -> str:
    """生成允许写进 `result`（因而会回给调用方）的错误摘要。

    见 `finish_error` 的说明：只有本系统自己写的文案才允许外流，
    外来异常的原文一律留在服务端日志里。
    """
    if isinstance(error, str):
        return error or "未知错误"

    if isinstance(error, AmrError):
        fields = error.context.get("fields")
        if isinstance(fields, (list, tuple)) and fields:
            return f"{error.message}（字段：{', '.join(str(item) for item in fields)}）"
        return error.message or type(error).__name__

    # 外来异常：类型名足够定位方向，原文（可能含 URL / 凭据 / 请求参数）不外流
    return f"{type(error).__name__}（详情见服务端日志）"
