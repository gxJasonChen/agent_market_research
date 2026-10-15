"""`JobRepository` 的**存储层边界语义**（spec/021 §1）。

这两条都是 Codex review 提出后补上的（M1/L1/L2），单独成文件是为了让「为什么有这两道护栏」
一眼可见——它们守的是源项目那个严重缺陷的形状，不是可有可无的防御。

用例编号：TC-C-011（占用语义 expected_from）、TC-C-012（写终态必须带 result）、
TC-C-013（list_recent 的排序确定性与 limit 语义）。
仍然跑**两个实现**：内存与 MySQL。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from amr.domain.errors import IllegalTransitionError, JobNotFoundError
from amr.domain.models import Job
from amr.domain.status import JobStatus

#: 契约夹具每个用例前都会清表（内存实现是全新实例），所以固定 ID 不会互相干扰
JOB_ID = "contract-guards-1"


def _make_job(job_id: str, now: datetime) -> Job:
    return Job(
        job_id=job_id,
        status=JobStatus.PENDING,
        customer_domain="openai.com",
        project_description="契约测试用的项目描述，长度足够",
        result=None,
        created_at=now,
        updated_at=now,
    )


# ------------------------------------------------------------------ TC-C-011


def test_expected_from_rejects_claim_when_already_running(repository) -> None:
    """TC-C-011 占用语义：作业已是 `RUNNING` 时，带 `expected_from=PENDING` 的写入必须失败（REQ-002）。

    这条守的是「重复投递会把整条 Flow 再跑一遍」：队列是 at-least-once 的，
    Worker 被 kill 后消息会重投；没有这道护栏时，`RUNNING → RUNNING` 在领域层是合法的
    幂等写入，于是第二次投递会拿到「成功」然后**再跑一遍**，双倍 token。
    """
    now = datetime.now(UTC)
    job = _make_job(JOB_ID, now)
    repository.create(job)
    repository.transition(JOB_ID, JobStatus.RUNNING, at=now)

    with pytest.raises(IllegalTransitionError) as exc:
        repository.transition(
            JOB_ID,
            JobStatus.RUNNING,
            at=now,
            expected_from=JobStatus.PENDING,
        )

    assert exc.value.context.get("guard") == "expected_from"
    assert exc.value.context.get("expected_from") == "PENDING"
    assert exc.value.context.get("from") == "RUNNING"


def test_expected_from_allows_first_claim(repository) -> None:
    """TC-C-011 补充：第一次认领（`PENDING` → `RUNNING`）正常通过（REQ-002）。"""
    now = datetime.now(UTC)
    repository.create(_make_job(JOB_ID, now))

    claimed = repository.transition(
        JOB_ID,
        JobStatus.RUNNING,
        at=now,
        expected_from=JobStatus.PENDING,
    )

    assert claimed.status is JobStatus.RUNNING


def test_expected_from_rejects_claim_on_terminal_job(repository) -> None:
    """TC-C-011 补充：终态作业同样认领不到（REQ-004）。"""
    now = datetime.now(UTC)
    repository.create(_make_job(JOB_ID, now))
    repository.transition(JOB_ID, JobStatus.RUNNING, at=now)
    repository.transition(JOB_ID, JobStatus.COMPLETE, result='{"ok": 1}', at=now)

    with pytest.raises(IllegalTransitionError):
        repository.transition(
            JOB_ID,
            JobStatus.RUNNING,
            at=now,
            expected_from=JobStatus.PENDING,
        )


# ------------------------------------------------------------------ TC-C-012


@pytest.mark.parametrize("target", [JobStatus.COMPLETE, JobStatus.ERROR])
def test_terminal_write_requires_result(repository, target: JobStatus) -> None:
    """TC-C-012 写终态必须带 `result`，否则直接拒绝（REQ-004）。

    `COMPLETE` 而 `result` 为空正是源项目「假成功」的症状形状：调用方看到完成，
    却拿不到任何结果。仓储层直接拒绝，不指望每个调用方都记得传 result。
    """
    now = datetime.now(UTC)
    repository.create(_make_job(JOB_ID, now))
    repository.transition(JOB_ID, JobStatus.RUNNING, at=now)

    with pytest.raises(IllegalTransitionError) as exc:
        repository.transition(JOB_ID, target, at=now)

    assert exc.value.context.get("guard") == "terminal_requires_result"
    # 库中状态保持不变，没有留下半个终态
    stored = repository.get(JOB_ID)
    assert stored is not None
    assert stored.status is JobStatus.RUNNING


def test_non_terminal_write_may_omit_result(repository) -> None:
    """TC-C-012 补充：非终态写入不要求 result（`RUNNING` 本来就没有结果）（REQ-004）。"""
    now = datetime.now(UTC)
    repository.create(_make_job(JOB_ID, now))

    job = repository.transition(JOB_ID, JobStatus.RUNNING, at=now)

    assert job.status is JobStatus.RUNNING
    assert job.result is None


def test_repository_returns_fresh_snapshot_after_terminal_write(repository) -> None:
    """TC-C-012 补充：返回值就是刚写进去的状态，不是过期快照（REQ-003）。

    并发下这一条才见真章：MySQL 实现用锁定读取最新已提交版本，
    普通一致性读会返回事务开始时的旧快照。
    """
    now = datetime.now(UTC)
    repository.create(_make_job(JOB_ID, now))
    repository.transition(JOB_ID, JobStatus.RUNNING, at=now)

    finished = repository.transition(JOB_ID, JobStatus.COMPLETE, result='{"ok": 1}', at=now)

    assert finished.status is JobStatus.COMPLETE
    assert finished.result == '{"ok": 1}'
    assert isinstance(finished, Job)


# ------------------------------------------------------------------ TC-C-013


def test_list_recent_limit_below_one_is_rejected(repository) -> None:
    """TC-C-013 `limit < 1` 两个实现必须同语义：抛 `ValueError`（spec/021 §1）。

    以前是内存返回 `[]`、MySQL 报 SQL 错（再被包装成 503）——同一份接口，
    同样的错误入参，两个实现给出完全不同的对外行为。
    """
    for bad in (0, -1, -100):
        with pytest.raises(ValueError):
            repository.list_recent(bad)


def test_list_recent_orders_by_created_at_desc(repository) -> None:
    """TC-C-013 按 `created_at` 倒序，且遵守 `limit`（spec/021 §1）。"""
    base = datetime(2026, 10, 15, 10, 0, 0, tzinfo=UTC)
    for index in range(3):
        moment = base.replace(minute=index)
        repository.create(_make_job(f"{JOB_ID}-{index}", moment))

    fresh = repository.list_recent(limit=2)

    assert [job.job_id for job in fresh] == [f"{JOB_ID}-2", f"{JOB_ID}-1"]
    assert all(job.events == () for job in fresh), "list_recent 不该带事件（避免 N+1）"


def test_list_recent_tie_break_is_deterministic(repository) -> None:
    """TC-C-013 `created_at` 并列时按 `job_id` 升序——第二排序键必须存在（spec/021 §1）。

    没有第二键时，内存实现按 dict 插入序、MySQL 按存储引擎返回序，
    两边都「看起来对」但互相对不上，契约测试就失去意义。
    """
    same_moment = datetime(2026, 10, 15, 10, 0, 0, tzinfo=UTC)
    for suffix in ("c", "a", "b"):
        repository.create(_make_job(f"{JOB_ID}-{suffix}", same_moment))

    ordered = repository.list_recent(limit=3)

    assert [job.job_id for job in ordered] == [f"{JOB_ID}-a", f"{JOB_ID}-b", f"{JOB_ID}-c"]


# ------------------------------------------------------------------ TC-C-014


def test_guard_order_is_the_same_in_both_implementations(repository) -> None:
    """TC-C-014 四道判定的**顺序**也是契约：同一个非法调用必须抛同一种异常（spec/021 §1）。

    顺序不一致时，调用方按异常类型分支就会「在内存实现上对、在 MySQL 上错」。
    这把顺序钉死为：存在性 → 迁移合法性 → 占用 → 终态带 result。
    """
    now = datetime.now(UTC)

    # 不存在的作业 + 写终态 + 没带 result → 必须先报「不存在」，而不是「终态没带 result」
    with pytest.raises(JobNotFoundError):
        repository.transition("definitely-missing", JobStatus.COMPLETE, at=now)

    # 存在但迁移本身非法 + 没带 result → 先报「非法迁移」
    repository.create(_make_job(JOB_ID, now))
    repository.transition(JOB_ID, JobStatus.RUNNING, at=now)
    repository.transition(JOB_ID, JobStatus.COMPLETE, result='{"ok": 1}', at=now)

    with pytest.raises(IllegalTransitionError) as exc:
        repository.transition(JOB_ID, JobStatus.ERROR, at=now)
    assert exc.value.context.get("guard") != "terminal_requires_result"


def test_mysql_stale_snapshot_cannot_claim_twice(mysql_repository) -> None:
    """TC-C-014 第一层判定读到**过期快照**时，占用语义仍必须挡住第二次认领（REQ-002）。

    这是复核发现的并发窗口：B 的 `SELECT` 早于 A 的 `commit`、B 的条件 UPDATE 晚于它时，
    UPDATE 不命中（rowcount=0），而重读看到的是 `RUNNING`——**非终态**。
    只看「是不是终态」的旧实现会把它判成「认领成功」，然后 B 把整条 Flow 再跑一遍。

    真库上这个窗口很难稳定命中，所以这里直接把第一层判定打桩成过期状态，
    **强制**走進 `rowcount==0` 那条分支。
    """
    from amr.adapters.mysql_repository import MySQLJobRepository

    now = datetime.now(UTC)
    mysql_repository.create(_make_job(JOB_ID, now))
    mysql_repository.transition(JOB_ID, JobStatus.RUNNING, at=now)  # 模拟 A 已认领

    # 让第一层判定看到「作业还是 PENDING」的过期快照
    original = MySQLJobRepository._read_status
    try:

        def _stale(cursor: object, job_id: str) -> JobStatus:  # noqa: ARG001 —— 桩要匹配签名
            return JobStatus.PENDING

        MySQLJobRepository._read_status = staticmethod(_stale)  # type: ignore[method-assign]
        with pytest.raises(IllegalTransitionError) as exc:
            mysql_repository.transition(
                JOB_ID,
                JobStatus.RUNNING,
                at=now,
                expected_from=JobStatus.PENDING,
            )
    finally:
        MySQLJobRepository._read_status = original  # type: ignore[method-assign]

    assert exc.value.context.get("guard") == "expected_from"
    stored = mysql_repository.get(JOB_ID)
    assert stored is not None and stored.status is JobStatus.RUNNING
