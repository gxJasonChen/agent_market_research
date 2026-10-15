"""`JobRepository` 契约测试（spec/021 §1.1、spec/040 §2.2）。

TC-C-001..010 覆盖 spec/021 §1.1 的十组断言，同一套用例参数化跑 `MemoryJobRepository`
与 `MySQLJobRepository`：两个实现的语义必须逐字一致，否则「换个实现就变行为」会在生产上
变成事故。TC-C-011..012 补的是存储故障语义（驱动异常不外泄、构造不连库），只在 MySQL
实现上有意义。

守护需求：REQ-003（查询）、REQ-004（状态机 / 终态不可变）、REQ-005（事件流水）、
REQ-011（错误语义）。
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone

import mysql.connector
import pytest

from amr.adapters import mysql_repository as mysql_repository_module
from amr.adapters.mysql_repository import MySQLJobRepository
from amr.config import Settings
from amr.domain.errors import IllegalTransitionError, JobNotFoundError, RepositoryUnavailableError
from amr.domain.models import Job
from amr.domain.status import JobStatus
from amr.ports.repository import JobRepository

# 带微秒的固定时间：往返丢精度会立刻被断言抓住
T0 = datetime(2026, 10, 15, 10, 0, 0, 123456, tzinfo=UTC)


def make_job(
    job_id: str = "job-1",
    *,
    created_at: datetime = T0,
    status: JobStatus = JobStatus.PENDING,
    result: str | None = None,
) -> Job:
    """构造一个作业快照的辅助函数。默认就是「刚受理」的形状。"""
    return Job(
        job_id=job_id,
        status=status,
        customer_domain="openai.com",
        project_description="为一家 AI 基础设施公司制定新品发布营销方案",
        result=result,
        created_at=created_at,
        updated_at=created_at,
    )


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


@contextmanager
def _closed_port() -> Iterator[int]:
    """占坑拿到一个「确定没人监听」的端口再立刻释放，用来制造「连不上」的场景。

    比写死端口可靠：写死的端口可能被别的进程占着，测试就会变成随机失败。
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    yield port


# ------------------------------------------------------------------ TC-C-011


def test_unreachable_database_raises_repository_unavailable() -> None:
    """TC-C-011 存储不可用：驱动异常包装成 RepositoryUnavailableError，且**构造时不连库**
    （spec/021 §1、REQ-011）。

    源项目的形状是「import 期就连库」，结果没有数据库连模块都 import 不了；这里连不上库时
    构造必须照样成功，只有真正读写才失败。异常信息里也绝不能出现口令（spec/000 §7）。
    """
    settings = Settings()
    with _closed_port() as port:
        dead = settings.model_copy(update={"mysql_host": "127.0.0.1", "mysql_port": port})

        repository = MySQLJobRepository(dead)  # 构造不连库：这一步抛异常就是回归

        with pytest.raises(RepositoryUnavailableError) as excinfo:
            repository.get("job-1")

    assert "connect" in excinfo.value.context.get("operation", "")
    if dead.mysql_password:  # 空口令时这条断言没有意义
        assert dead.mysql_password not in str(excinfo.value)


# ------------------------------------------------------------------ TC-C-012


class _BrokenCursor:
    """`execute` 就抛驱动异常的游标：模拟「连接建起来了、语句执行时存储挂了」。"""

    def __init__(self, error: mysql.connector.Error) -> None:
        self._error = error

    def __enter__(self) -> _BrokenCursor:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, *args: object, **kwargs: object) -> None:
        raise self._error


class _BrokenConnection:
    """只提供 `cursor` / `close` 的连接替身。"""

    def __init__(self, error: mysql.connector.Error) -> None:
        self._error = error

    def cursor(self) -> _BrokenCursor:
        return _BrokenCursor(self._error)

    def close(self) -> None:
        return None


_FAILING_CALLS: dict[str, Callable[[MySQLJobRepository], object]] = {
    "create": lambda repository: repository.create(make_job()),
    "get": lambda repository: repository.get("job-1"),
    "append_event": lambda repository: repository.append_event("job-1", "文本", at=T0),
    "transition": lambda repository: repository.transition("job-1", JobStatus.RUNNING, at=T0),
    "list_recent": lambda repository: repository.list_recent(),
}


@pytest.mark.parametrize("operation", list(_FAILING_CALLS))
def test_driver_failure_is_wrapped_per_operation(
    operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-C-012 驱动异常不外泄：每个方法都要把 `mysql.connector.Error` 包成
    `RepositoryUnavailableError`（spec/021 §1，REQ-011）。

    漏掉任意一个方法，上层就会看到驱动异常 —— HTTP 层认不出它，会直接 500 并把堆栈
    里的连接信息泼到响应体里。所以逐个方法钉住。
    """
    error = mysql.connector.OperationalError("MySQL Connection not available")
    monkeypatch.setattr(MySQLJobRepository, "_connect", lambda _self: _BrokenConnection(error))
    monkeypatch.setattr(MySQLJobRepository, "ensure_schema", lambda _self: None)
    repository = MySQLJobRepository(Settings())

    with pytest.raises(RepositoryUnavailableError) as excinfo:
        _FAILING_CALLS[operation](repository)

    assert excinfo.value.context["operation"] == operation


# ------------------------------------------------------------------ TC-C-001


def test_create_then_get_round_trips(repository: JobRepository) -> None:
    """TC-C-001 创建与读取：create 后 get 得到等值作业，状态 PENDING、events 为空（REQ-001）。"""
    job = make_job()

    repository.create(job)

    stored = repository.get("job-1")
    assert stored == job
    assert stored is not None
    assert stored.status is JobStatus.PENDING
    assert stored.events == ()
    assert stored.result is None


def test_timestamps_round_trip_as_aware_utc(repository: JobRepository) -> None:
    """TC-C-001 补充：时间戳往返后仍是**带时区**的，且与写入时是同一时刻（REQ-003）。

    源项目存 naive 本地时间，前端按浏览器时区渲染后整体偏移 —— 这条断言就是那道防线。
    """
    # 东八区 18:00 就是 UTC 10:00；不是 UTC 的输入也必须归一化成同一时刻
    other_zone = datetime(2026, 10, 15, 18, 0, 0, 500000, tzinfo=timezone(timedelta(hours=8)))
    repository.create(make_job(created_at=other_zone))

    stored = repository.get("job-1")

    assert stored is not None
    assert stored.created_at.tzinfo is not None  # 绝不能是 naive
    assert stored.created_at == other_zone  # 跨时区比较的是同一时刻
    assert stored.updated_at == stored.created_at


# ------------------------------------------------------------------ TC-C-002


def test_create_rejects_duplicate_job_id(repository: JobRepository) -> None:
    """TC-C-002 重复创建：同 job_id 二次 create 抛 ValueError（REQ-001）。"""
    repository.create(make_job())

    with pytest.raises(ValueError):
        repository.create(make_job())

    stored = repository.get("job-1")
    assert stored is not None
    assert stored.project_description == make_job().project_description


# ------------------------------------------------------------------ TC-C-003


def test_get_returns_none_for_unknown_job(repository: JobRepository) -> None:
    """TC-C-003 读取不存在：返回 None，**不是**抛异常（REQ-003）。"""
    assert repository.get("does-not-exist") is None


# ------------------------------------------------------------------ TC-C-004


def test_append_event_rejects_unknown_job(repository: JobRepository) -> None:
    """TC-C-004 事件追加：对不存在的作业追加抛 JobNotFoundError（REQ-005）。

    不隐式建作业 —— 否则外键形同虚设，孤立事件会悄悄堆积。
    """
    with pytest.raises(JobNotFoundError):
        repository.append_event("ghost", "不该落地的文本", at=T0)


def test_append_event_returns_events_in_time_order(repository: JobRepository) -> None:
    """TC-C-004 事件追加：按时间正序返回，且只返回该作业的事件（REQ-005）。"""
    repository.create(make_job("job-1"))
    repository.create(make_job("job-2", created_at=_at(1)))

    repository.append_event("job-1", "受理", at=_at(1))
    repository.append_event("job-2", "别的作业的事件", at=_at(2))
    repository.append_event("job-1", "入队", at=_at(3))

    job = repository.get("job-1")

    assert job is not None
    assert [event.data for event in job.events] == ["受理", "入队"]
    assert [event.timestamp for event in job.events] == [_at(1), _at(3)]


def test_append_event_keeps_insertion_order_for_equal_timestamps(
    repository: JobRepository,
) -> None:
    """TC-C-004 补充：同一时刻的多条事件保持写入顺序（REQ-005）。

    这不是吹毛求疵：`JobService.create` 在同一个冻结时刻写「已受理」「已入队」两条事件，
    顺序错了前端的流水读起来就是反的。
    """
    repository.create(make_job())
    repository.append_event("job-1", "第一条", at=_at(1))
    repository.append_event("job-1", "第二条", at=_at(1))

    job = repository.get("job-1")

    assert job is not None
    assert [event.data for event in job.events] == ["第一条", "第二条"]


# ------------------------------------------------------------------ TC-C-005


def test_transition_walks_pending_running_complete(repository: JobRepository) -> None:
    """TC-C-005 合法迁移：PENDING→RUNNING→COMPLETE 逐步一致，result 只在终态写入（REQ-004）。"""
    repository.create(make_job())

    running = repository.transition("job-1", JobStatus.RUNNING, at=_at(1))
    assert running.status is JobStatus.RUNNING
    assert running.result is None  # 终态前 result 必须为空
    assert running.updated_at == _at(1)
    assert running.created_at == T0  # created_at 不因迁移而变
    assert repository.get("job-1") == running  # 返回的快照与库中一致

    complete = repository.transition("job-1", JobStatus.COMPLETE, result='{"ok": true}', at=_at(2))
    assert complete.status is JobStatus.COMPLETE
    assert complete.result == '{"ok": true}'
    assert complete.updated_at == _at(2)
    assert repository.get("job-1") == complete


def test_transition_keeps_events_and_result_of_earlier_steps(repository: JobRepository) -> None:
    """TC-C-005 补充：迁移返回的快照带上事件流水（与内存实现同语义，REQ-005）。"""
    repository.create(make_job())
    repository.append_event("job-1", "Flow 开始执行", at=_at(1))

    job = repository.transition("job-1", JobStatus.RUNNING, at=_at(2))

    assert [event.data for event in job.events] == ["Flow 开始执行"]


# ------------------------------------------------------------------ TC-C-006


def test_transition_from_error_to_complete_is_rejected(repository: JobRepository) -> None:
    """TC-C-006 **失败作业不能被改写成成功**：ERROR→COMPLETE 抛 IllegalTransitionError，
    且库里仍是 ERROR、错误摘要原样保留（REQ-004）。

    源项目严重缺陷的回归测试：异常分支写了 ERROR 之后，成功分支又无条件写 COMPLETE。
    """
    repository.create(make_job())
    repository.transition("job-1", JobStatus.RUNNING, at=_at(1))
    repository.transition("job-1", JobStatus.ERROR, result="调研失败：工具异常", at=_at(2))

    with pytest.raises(IllegalTransitionError):
        repository.transition("job-1", JobStatus.COMPLETE, result='{"假的": "成功"}', at=_at(3))

    stored = repository.get("job-1")
    assert stored is not None
    assert stored.status is JobStatus.ERROR
    assert stored.result == "调研失败：工具异常"  # 结果也没被覆盖
    assert stored.updated_at == _at(2)


def test_complete_is_also_terminal(repository: JobRepository) -> None:
    """TC-C-006 补充：终态对称 —— COMPLETE 之后写 ERROR 同样被拒（REQ-004）。"""
    repository.create(make_job())
    repository.transition("job-1", JobStatus.RUNNING, at=_at(1))
    repository.transition("job-1", JobStatus.COMPLETE, result='{"ok": true}', at=_at(2))

    with pytest.raises(IllegalTransitionError):
        repository.transition("job-1", JobStatus.ERROR, result="事后补记失败", at=_at(3))

    stored = repository.get("job-1")
    assert stored is not None
    assert stored.status is JobStatus.COMPLETE
    assert stored.result == '{"ok": true}'


def test_conditional_update_blocks_terminal_overwrite(
    mysql_clone_factory: Callable[[], MySQLJobRepository],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-C-006 补充：两层防护的**第二层**（条件 UPDATE）单独也挡得住写终态（REQ-004）。

    单线程用例走不到这条路 —— 第一层判定通过之后、UPDATE 之前，作业才被另一个进程改成
    终态。这里用 monkeypatch 在第一层判定里插入「另一个连接写入 ERROR 并提交」，精确复现，
    并断言异常来自第二层（`guard=conditional_update`）：第一层根本没看出问题。
    """
    repository = mysql_clone_factory()
    peer = mysql_clone_factory()  # 另一条独立连接 = 另一个 Worker 进程
    repository.create(make_job())
    repository.transition("job-1", JobStatus.RUNNING, at=_at(1))

    real_can_transition = mysql_repository_module.can_transition
    raced = False

    def racing_can_transition(src: JobStatus, dst: JobStatus) -> bool:
        nonlocal raced
        if not raced:
            raced = True
            # 并发者：另一个 Worker 在两层之间把作业推到终态并提交
            peer.transition("job-1", JobStatus.ERROR, result="并发失败", at=_at(2))
            return True  # 第一层「放行」，把防线交给条件 UPDATE
        return real_can_transition(src, dst)

    monkeypatch.setattr(mysql_repository_module, "can_transition", racing_can_transition)

    with pytest.raises(IllegalTransitionError) as excinfo:
        repository.transition("job-1", JobStatus.COMPLETE, result="假成功", at=_at(3))

    assert excinfo.value.context.get("guard") == "conditional_update"
    stored = repository.get("job-1")
    assert stored is not None
    assert stored.status is JobStatus.ERROR
    assert stored.result == "并发失败"


# ------------------------------------------------------------------ TC-C-007


def test_transition_to_same_non_terminal_status_is_idempotent(
    repository: JobRepository,
) -> None:
    """TC-C-007 幂等：PENDING→PENDING 成功且不抛异常（REQ-004，Worker 重投递场景）。"""
    repository.create(make_job())

    again = repository.transition("job-1", JobStatus.PENDING, at=_at(1))

    assert again.status is JobStatus.PENDING
    assert again.updated_at == _at(1)
    stored = repository.get("job-1")
    assert stored is not None
    assert stored.status is JobStatus.PENDING


def test_idempotent_transition_with_unchanged_values_still_succeeds(
    repository: JobRepository,
) -> None:
    """TC-C-007 补充：重复写入且**值完全没变**时同样是成功（REQ-004）。

    这一格专门盯着条件 UPDATE 的 `rowcount`：MySQL 对「匹配到但没有实际改动」的行返回 0，
    这和「一行都没匹配到」是两回事，必须区分开，否则幂等重投递会被误判成并发冲突。
    """
    repository.create(make_job())

    again = repository.transition("job-1", JobStatus.PENDING, at=T0)

    assert again.status is JobStatus.PENDING
    assert again.updated_at == T0
    stored = repository.get("job-1")
    assert stored is not None
    assert stored.status is JobStatus.PENDING


# ------------------------------------------------------------------ TC-C-008


def test_transition_running_to_pending_is_rejected(repository: JobRepository) -> None:
    """TC-C-008 非法迁移：RUNNING→PENDING 抛 IllegalTransitionError（REQ-004）。"""
    repository.create(make_job())
    repository.transition("job-1", JobStatus.RUNNING, at=_at(1))

    with pytest.raises(IllegalTransitionError):
        repository.transition("job-1", JobStatus.PENDING, at=_at(2))

    stored = repository.get("job-1")
    assert stored is not None
    assert stored.status is JobStatus.RUNNING


# ------------------------------------------------------------------ TC-C-009


def test_transition_unknown_job_raises_not_found(repository: JobRepository) -> None:
    """TC-C-009 不存在的作业：transition 抛 JobNotFoundError（REQ-004）。"""
    with pytest.raises(JobNotFoundError):
        repository.transition("ghost", JobStatus.RUNNING, at=_at(1))


# ------------------------------------------------------------------ TC-C-010


def test_list_recent_orders_by_created_at_desc_and_honours_limit(
    repository: JobRepository,
) -> None:
    """TC-C-010 最近列表：按 created_at 倒序且遵守 limit（REQ-003）。"""
    for index in range(1, 6):
        repository.create(make_job(f"job-{index}", created_at=_at(index)))

    recent = repository.list_recent(3)

    assert [job.job_id for job in recent] == ["job-5", "job-4", "job-3"]
    assert [job.created_at for job in recent] == [_at(5), _at(4), _at(3)]
    assert [job.job_id for job in repository.list_recent(10)] == [
        "job-5",
        "job-4",
        "job-3",
        "job-2",
        "job-1",
    ]


def test_list_recent_omits_events(repository: JobRepository) -> None:
    """TC-C-010 补充：列表不带事件流水，避免 N+1 查询（REQ-005）。"""
    repository.create(make_job())
    repository.append_event("job-1", "受理", at=_at(1))

    recent = repository.list_recent()

    assert len(recent) == 1
    assert recent[0].events == ()
