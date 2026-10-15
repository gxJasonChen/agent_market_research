"""执行器的占用语义与收尾重试（Codex review M1 / M2）。

守护需求：REQ-002（异步执行）、REQ-004（状态机）。

这两条都是 review 发现的**真实缺口**：

- M1：`RUNNING` 中的重复投递不会被拦（`RUNNING → RUNNING` 在领域层是合法幂等），
  于是整条 Flow 会再跑一遍——双倍 token；
- M2：收尾写库失败会让作业永久停在 `RUNNING`，而 `PENDING` 侧有防护、`RUNNING` 侧没有。

用例编号：TC-U-030。
"""

from __future__ import annotations

import pytest

from amr.adapters.repository_publisher import RepositoryEventPublisher
from amr.domain.errors import AmrError, RepositoryUnavailableError
from amr.domain.inputs import CrewInputs
from amr.domain.status import JobStatus
from amr.services.job_service import JobService
from amr.services.orchestrator import FlowExecutor
from tests.fakes import (
    FakeFlowRunner,
    FrozenClock,
    RecordingJobRepository,
    RecordingTaskQueue,
    SeqIdGenerator,
)

INPUTS = CrewInputs(
    customer_domain="openai.com",
    project_description="为一家 AI 基础设施公司制定新品发布营销方案",
)


def _harness(
    runner: FakeFlowRunner | None = None,
    *,
    repository: RecordingJobRepository | None = None,
    finalize_attempts: int = 3,
    backoff: float = 0.0,
) -> tuple[FlowExecutor, JobService, RecordingJobRepository]:
    repo = repository or RecordingJobRepository()
    clock = FrozenClock()
    publisher = RepositoryEventPublisher(repository=repo, clock=clock)
    service = JobService(
        repository=repo,
        queue=RecordingTaskQueue(),
        clock=clock,
        ids=SeqIdGenerator(),
        publisher=publisher,
    )
    executor = FlowExecutor(
        service=service,
        runner=runner or FakeFlowRunner(),
        publisher=publisher,
        finalize_attempts=finalize_attempts,
        finalize_backoff_seconds=backoff,
    )
    return executor, service, repo


def _enqueue(service: JobService) -> str:
    return service.create(INPUTS)


# ------------------------------------------------------------------ TC-U-030 占用


def test_duplicate_delivery_while_running_does_not_rerun_the_flow() -> None:
    """TC-U-030 作业已在 `RUNNING` 时，重复投递**不得**再跑一遍 Flow（REQ-002）。

    这是 M1 的回归测试。没有占用语义时，`mark_running` 会把 `RUNNING → RUNNING`
    当合法幂等写入，于是第二次投递顺利拿到「成功」并再跑一遍。
    """
    runner = FakeFlowRunner()
    executor, service, repo = _harness(runner)
    job_id = _enqueue(service)

    # 第一次：正常认领并跑完
    service.mark_running(job_id)
    assert repo.get(job_id).status is JobStatus.RUNNING  # type: ignore[union-attr]

    # 第二次投递（模拟 Worker 崩溃后的消息重投）：拿不到占用，安静跳过
    executor.execute(job_id, INPUTS)

    assert runner.calls == [], "重复投递时 Runner 被调用了——Flow 会再跑一遍"


def test_duplicate_delivery_after_terminal_does_not_rerun() -> None:
    """TC-U-030 终态作业上的重复投递同样跳过（REQ-004）。"""
    runner = FakeFlowRunner()
    executor, service, _ = _harness(runner)
    job_id = _enqueue(service)
    executor.execute(job_id, INPUTS)

    executor.execute(job_id, INPUTS)

    assert len(runner.calls) == 1


def test_first_delivery_still_runs() -> None:
    """TC-U-030 补充：占用语义不能把正常路径也挡了（REQ-002）。"""
    runner = FakeFlowRunner()
    executor, service, repo = _harness(runner)
    job_id = _enqueue(service)

    executor.execute(job_id, INPUTS)

    assert len(runner.calls) == 1
    assert repo.get(job_id).status is JobStatus.COMPLETE  # type: ignore[union-attr]


# ------------------------------------------------------------------ TC-U-030 收尾重试


class _FlakyRepository(RecordingJobRepository):
    """前 N 次 `transition` 到终态抛存储不可用，之后恢复正常。"""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self._remaining = failures
        self.attempts = 0
        self.seen: list[str] = []

    def transition(self, job_id, to, *, result=None, at, expected_from=None):  # type: ignore[no-untyped-def]
        if to in (JobStatus.COMPLETE, JobStatus.ERROR):
            self.attempts += 1
            self.seen.append(to.value)
            if self._remaining > 0:
                self._remaining -= 1
                raise RepositoryUnavailableError("存储不可用（transition）")
        return super().transition(job_id, to, result=result, at=at, expected_from=expected_from)


def test_transient_storage_failure_during_finalize_is_retried() -> None:
    """TC-U-030 收尾时的一次存储抖动应被重试吸收，作业正常落成 `COMPLETE`（REQ-004）。

    这是 M2 的回归测试：不重试的话作业会永久停在 `RUNNING`。
    """
    repo = _FlakyRepository(failures=2)
    executor, service, _ = _harness(repository=repo)
    job_id = _enqueue(service)

    executor.execute(job_id, INPUTS)

    assert repo.attempts == 3, "前两次失败后应重试到第三次"
    assert repo.get(job_id).status is JobStatus.COMPLETE  # type: ignore[union-attr]


def test_persistent_storage_failure_is_raised_not_swallowed() -> None:
    """TC-U-030 重试耗尽后**必须抛出去**，绝不吞掉（REQ-004）。

    吞掉等于把「作业卡在 RUNNING 且无人知晓」变成常态；抛出去至少 Celery 会记失败，
    还有 `scripts/reap_stuck_jobs.py` 兜底。
    """
    repo = _FlakyRepository(failures=99)
    executor, service, _ = _harness(repository=repo, finalize_attempts=2)
    job_id = _enqueue(service)

    with pytest.raises(RepositoryUnavailableError):
        executor.execute(job_id, INPUTS)

    assert repo.attempts == 2
    assert repo.get(job_id).status is JobStatus.RUNNING  # type: ignore[union-attr]


def test_finalize_retry_does_not_rerun_the_flow() -> None:
    """TC-U-030 重试的是**写入**不是整条 Flow——两者代价差几个数量级（REQ-002）。"""
    repo = _FlakyRepository(failures=2)
    runner = FakeFlowRunner()
    executor, service, _ = _harness(runner, repository=repo)
    job_id = _enqueue(service)

    executor.execute(job_id, INPUTS)

    assert len(runner.calls) == 1, "Flow 被重跑了——收尾重试不应该牵动执行"


def test_finalize_does_not_retry_on_programming_errors() -> None:
    """TC-U-030 只重试**暂时性**存储故障；非法迁移这类错误一次都不该重试（REQ-004）。"""

    class _TerminalConflict(RecordingJobRepository):
        def __init__(self) -> None:
            super().__init__()
            self.seen = 0

        def transition(self, job_id, to, *, result=None, at, expected_from=None):  # type: ignore[no-untyped-def]
            if to is JobStatus.COMPLETE:
                self.seen += 1
                from amr.domain.errors import IllegalTransitionError

                raise IllegalTransitionError(
                    "作业已成终态", context={"guard": "conditional_update"}
                )
            return super().transition(job_id, to, result=result, at=at, expected_from=expected_from)

    repo = _TerminalConflict()
    executor, service, _ = _harness(repository=repo)
    job_id = _enqueue(service)

    with pytest.raises(AmrError):
        executor.execute(job_id, INPUTS)

    assert repo.seen == 1, "非法迁移被重试了——重试掩盖并发冲突，且拖长 Worker 占用"


def test_finalize_treats_already_at_target_as_success() -> None:
    """TC-U-030 收尾写入**已提交但应答丢失**时，重试必须当成功，而不是记任务失败（REQ-004）。

    复核发现的场景：`transition` 在服务端提交成功、`commit()` 的应答在路上丢了 →
    抛 `RepositoryUnavailableError` → 重试 → 第二次读到终态 → 「终态不可重写」。
    数据其实是对的（作业已 COMPLETE），若把它当失败，指标和日志都会说谎，
    运维还会去查一个并不存在的问题。
    """

    class _CommitAckLost(RecordingJobRepository):
        """第一次写终态：数据真的写进去了，但抛「存储不可用」模拟应答丢失。"""

        def __init__(self) -> None:
            super().__init__()
            self.wrote = False

        def transition(self, job_id, to, *, result=None, at, expected_from=None):  # type: ignore[no-untyped-def]
            if to is JobStatus.COMPLETE and not self.wrote:
                self.wrote = True
                super().transition(job_id, to, result=result, at=at, expected_from=expected_from)
                raise RepositoryUnavailableError("commit 应答丢失")
            return super().transition(job_id, to, result=result, at=at, expected_from=expected_from)

    repo = _CommitAckLost()
    executor, service, _ = _harness(repository=repo)
    job_id = _enqueue(service)

    executor.execute(job_id, INPUTS)  # 不应抛异常

    stored = repo.get(job_id)
    assert stored is not None
    assert stored.status is JobStatus.COMPLETE
    assert stored.result is not None


def test_finalize_still_raises_when_job_is_not_at_target() -> None:
    """TC-U-030 补充：作业**不在**目标终态时，重试耗尽仍然必须抛（别把幂等确认放宽成吞异常）。"""
    repo = _FlakyRepository(failures=99)
    executor, service, _ = _harness(repository=repo, finalize_attempts=2)
    job_id = _enqueue(service)

    with pytest.raises(RepositoryUnavailableError):
        executor.execute(job_id, INPUTS)
