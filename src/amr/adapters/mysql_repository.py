"""`JobRepository` 的 MySQL 实现（spec/021-contracts-ports.md §1）。

五条设计约束，每一条都对应源项目的一个具体故障：

1. **没有模块级连接。** 每次操作开一条独立连接、`finally` 里关闭。源项目在 import 期就连库，
   后果是「没有数据库连模块都 import 不了」—— 单元测试、打包、CI 全被拖死。
   事务收尾也简单：写完 `commit()`，失败时不显式回滚，交给 `close()` 隐式回滚。
2. **时间一律 tz-aware UTC。** 写库前 `astimezone(UTC)` 再剥掉时区（列是 `DATETIME(6)`，
   MySQL 不存时区），读出来 `.replace(tzinfo=UTC)` 挂回去。源项目存 naive 本地时间，
   跨时区会整体偏移。
3. **两层终态防护。** 第一层用 `domain.status.can_transition`（规则只有一份，见 spec/020 §1.2）
   给出明确的领域异常；第二层用条件 `UPDATE ... WHERE status NOT IN ('COMPLETE','ERROR')`
   加 `rowcount` 校验兜住并发 —— 两个 Worker 同时收尾时，这是终态不被覆盖的最后一道防线
   （REQ-004）。
4. **条件更新后必须重读校验。** `rowcount == 0` 有三种可能（作业被删 / 状态已被并发写成终态 /
   值本来就没变），必须用 `SELECT ... FOR UPDATE` 读**最新已提交**版本才能分清：第一层判定
   用的是普通一致性读，在 REPEATABLE READ 下它看到的是旧快照，拿它判断并发结果会漏判。
5. **驱动异常不外泄。** `mysql.connector.Error` 一律包装成 `RepositoryUnavailableError`；
   唯一例外是主键冲突 —— 端口契约规定重复 `create` 抛 `ValueError`。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from threading import Lock
from typing import TYPE_CHECKING, Any, cast

import mysql.connector

from amr.config import Settings
from amr.domain.errors import (
    IllegalTransitionError,
    JobNotFoundError,
    RepositoryUnavailableError,
)
from amr.domain.models import Event, Job
from amr.domain.status import JobStatus, can_transition, is_terminal

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查；运行时用不到这几个名字
    from mysql.connector.abstracts import MySQLConnectionAbstract, MySQLCursorAbstract
    from mysql.connector.pooling import PooledMySQLConnection

__all__ = ["MySQLJobRepository"]

logger = logging.getLogger(__name__)

#: ER_DUP_ENTRY。端口契约要求「重复 create」表现为 `ValueError`，不是存储故障。
_ER_DUP_ENTRY = 1062

_DDL_JOBS = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id VARCHAR(64) PRIMARY KEY,
    status VARCHAR(16) NOT NULL,
    customer_domain VARCHAR(255) NOT NULL,
    project_description TEXT NOT NULL,
    result MEDIUMTEXT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL
) ENGINE=InnoDB CHARSET=utf8mb4
"""

_DDL_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    job_id VARCHAR(64) NOT NULL,
    `timestamp` DATETIME(6) NOT NULL,
    data TEXT NOT NULL,
    KEY idx_events_job (job_id, id),
    CONSTRAINT fk_events_job FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
) ENGINE=InnoDB CHARSET=utf8mb4
"""

_INSERT_JOB = (
    "INSERT INTO jobs"
    " (job_id, status, customer_domain, project_description, result, created_at, updated_at)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s)"
)

_SELECT_JOB = (
    "SELECT job_id, status, customer_domain, project_description, result, created_at, updated_at"
    " FROM jobs WHERE job_id=%s"
)

#: `id` 是同秒同微秒事件之间的第二排序键：JobService 在同一个冻结时刻写两条事件是常态。
_SELECT_EVENTS = "SELECT `timestamp`, data FROM events WHERE job_id=%s ORDER BY `timestamp`, id"

_SELECT_RECENT = (
    "SELECT job_id, status, customer_domain, project_description, result, created_at, updated_at"
    " FROM jobs ORDER BY created_at DESC, job_id ASC LIMIT %s"
)

#: 第二层防护。终态集合直接写在这条语句里是有意的：**判定必须与写入是同一条语句**，
#: 「先查再写」之间必然存在窗口（spec/021 §1 把这条 SQL 写死在契约里）。
#: `AND (%s IS NULL OR status = %s)` 是**占用语义**：调用方给了 expected_from 时，
#: 只有状态仍是它才写。这样 `_read_status` 读到过期快照也不会误写。
_UPDATE_STATUS = (
    "UPDATE jobs SET status=%s, result=COALESCE(%s, result), updated_at=%s"
    " WHERE job_id=%s AND status NOT IN ('COMPLETE','ERROR')"
    " AND (%s IS NULL OR status = %s)"
)


class MySQLJobRepository:
    """MySQL 存储实现，表在首次使用时按需创建（`IF NOT EXISTS`）。

    构造这个类**不连库**，连接参数只从 `amr.config.Settings` 读 ——
    没有数据库时，import 与构造都必须照常成功。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()
        self._schema_lock = Lock()
        self._schema_ready = False

    # ------------------------------------------------------------------ DDL

    def ensure_schema(self) -> None:
        """建表（幂等、线程安全）。首次使用时自动调用，也可由运维/测试夹具显式调用。"""
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:  # 双重检查：另一个线程可能已经建好了
                return
            connection = self._connect()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(_DDL_JOBS)
                    cursor.execute(_DDL_EVENTS)
                connection.commit()
            except mysql.connector.Error as exc:
                raise self._unavailable(exc, "ensure_schema") from exc
            finally:
                connection.close()
            self._schema_ready = True

    # ------------------------------------------------------- JobRepository

    def create(self, job: Job) -> None:
        """插入作业。`job_id` 已存在时抛 `ValueError`。"""
        self.ensure_schema()
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    _INSERT_JOB,
                    (
                        job.job_id,
                        job.status.value,
                        job.customer_domain,
                        job.project_description,
                        job.result,
                        _to_db(job.created_at),
                        _to_db(job.updated_at),
                    ),
                )
            connection.commit()
        except mysql.connector.IntegrityError as exc:
            if exc.errno == _ER_DUP_ENTRY:
                # 调用方的错，不是存储故障 —— 唯独这一类不包装成 RepositoryUnavailableError
                raise ValueError(f"作业已存在: {job.job_id}") from exc
            raise self._unavailable(exc, "create", job_id=job.job_id) from exc
        except mysql.connector.Error as exc:
            raise self._unavailable(exc, "create", job_id=job.job_id) from exc
        finally:
            connection.close()

    def get(self, job_id: str) -> Job | None:
        """返回作业及其事件（按时间正序）。不存在返回 `None`，不抛异常。"""
        self.ensure_schema()
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                return self._read_job(cursor, job_id)
        except mysql.connector.Error as exc:
            raise self._unavailable(exc, "get", job_id=job_id) from exc
        finally:
            connection.close()

    def append_event(self, job_id: str, data: str, *, at: datetime) -> None:
        """追加一条事件。作业不存在抛 `JobNotFoundError`（不隐式建作业）。"""
        self.ensure_schema()
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                if not self._exists(cursor, job_id):
                    # 外键本来也能挡住，但 1452 说不清「谁不存在」；这里先给明确语义
                    raise JobNotFoundError("作业不存在，无法追加事件", context={"job_id": job_id})
                cursor.execute(
                    "INSERT INTO events (job_id, `timestamp`, data) VALUES (%s, %s, %s)",
                    (job_id, _to_db(at), data),
                )
            connection.commit()
        except mysql.connector.Error as exc:
            raise self._unavailable(exc, "append_event", job_id=job_id) from exc
        finally:
            connection.close()

    def transition(
        self,
        job_id: str,
        to: JobStatus,
        *,
        result: str | None = None,
        at: datetime,
        expected_from: JobStatus | None = None,
    ) -> Job:
        """状态迁移，返回迁移后的作业快照（含事件）。

        - 作业不存在 → `JobNotFoundError`
        - 迁移非法（含写入终态）→ `IllegalTransitionError`，库中状态保持不变
        - `expected_from` 与当前状态不符 → `IllegalTransitionError`（占用语义）
        - 写终态但 `result` 为空 → `IllegalTransitionError`（源项目「假成功」的症状形状）
        - 同一非终态重复写入 → 幂等成功（`result` 传入时才更新）
        """
        # 先按域名规则挡住「写终态却没有结果」——这正是源项目假成功的形状，
        # 不能指望每个调用方都记得传 result（仓储是最后一道，不是唯一一道）
        if is_terminal(to) and result is None:
            raise IllegalTransitionError(
                "写入终态必须带 result",
                context={"job_id": job_id, "to": to.value, "guard": "terminal_requires_result"},
            )

        self.ensure_schema()
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                current = self._read_status(cursor, job_id)
                if current is None:
                    raise JobNotFoundError("作业不存在，无法迁移状态", context={"job_id": job_id})
                if not can_transition(current, to):
                    raise IllegalTransitionError(
                        "非法的状态迁移",
                        context={"job_id": job_id, "from": current.value, "to": to.value},
                    )
                if expected_from is not None and current is not expected_from:
                    raise IllegalTransitionError(
                        "状态与预期不符，迁移作废",
                        context={
                            "job_id": job_id,
                            "from": current.value,
                            "to": to.value,
                            "expected_from": expected_from.value,
                            "guard": "expected_from",
                        },
                    )

                expected = expected_from.value if expected_from is not None else None
                cursor.execute(
                    _UPDATE_STATUS,
                    (to.value, result, _to_db(at), job_id, expected, expected),
                )
                if cursor.rowcount == 0:
                    self._raise_if_blocked(cursor, job_id, to)

                # 用**锁定读**取返回快照：普通一致性读在 REPEATABLE READ 下看的是事务开始时的
                # 快照，并发刚提交的终态它看不见，会把过期状态当结果返回给调用方。
                updated = self._read_job(cursor, job_id, for_update=True)
                if updated is None:  # 两次读之间被删：宁可报「不存在」也不返回幽灵快照
                    raise JobNotFoundError("作业不存在，无法迁移状态", context={"job_id": job_id})
            connection.commit()
            return updated
        except mysql.connector.Error as exc:
            raise self._unavailable(exc, "transition", job_id=job_id) from exc
        finally:
            connection.close()

    def list_recent(self, limit: int = 20) -> list[Job]:
        """按 `created_at` 倒序返回最近作业（不带事件，避免 N+1 查询）。

        `limit < 1` 直接抛 `ValueError`：否则 `LIMIT -1` 会变成一条 SQL 错误，
        被包装成 `RepositoryUnavailableError` → 503，把「调用方传错参数」
        伪装成「存储不可用」。两个实现必须同语义（spec/021 §1）。
        """
        if limit < 1:
            raise ValueError(f"limit 必须大于 0，收到 {limit}")

        self.ensure_schema()
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(_SELECT_RECENT, (limit,))
                rows = cursor.fetchall()
            return [_row_to_job(row, ()) for row in rows]
        except mysql.connector.Error as exc:
            raise self._unavailable(exc, "list_recent") from exc
        finally:
            connection.close()

    # ------------------------------------------------------------ 内部实现

    def _connect(self) -> MySQLConnectionAbstract | PooledMySQLConnection:
        """开一条独立连接。参数只来自 `Settings`，失败即 `RepositoryUnavailableError`。"""
        try:
            return mysql.connector.connect(**self._settings.mysql_config)
        except mysql.connector.Error as exc:
            raise self._unavailable(exc, "connect") from exc

    @staticmethod
    def _fetch_row(cursor: MySQLCursorAbstract) -> tuple[Any, ...] | None:
        """取一行并收窄类型。

        驱动把返回值标成 `tuple | dict`（`dictionary=True` 时是 dict）。本实现一律用默认
        cursor，运行时必然是 tuple —— 在这里收窄一次，下游所有取值都是干净的。
        """
        row = cursor.fetchone()
        return None if row is None else cast("tuple[Any, ...]", row)

    @staticmethod
    def _fetch_rows(cursor: MySQLCursorAbstract) -> list[tuple[Any, ...]]:
        """取全部行并收窄类型，理由同 `_fetch_row`。"""
        return [cast("tuple[Any, ...]", row) for row in cursor.fetchall()]

    @staticmethod
    def _exists(cursor: MySQLCursorAbstract, job_id: str) -> bool:
        cursor.execute("SELECT 1 FROM jobs WHERE job_id=%s", (job_id,))
        return cursor.fetchone() is not None

    @staticmethod
    def _read_status(cursor: MySQLCursorAbstract, job_id: str) -> JobStatus | None:
        """第一层判定用的状态读。普通一致性读即可：它的作用只是尽早给出明确错误。"""
        cursor.execute("SELECT status FROM jobs WHERE job_id=%s", (job_id,))
        row = MySQLJobRepository._fetch_row(cursor)
        return None if row is None else JobStatus(str(row[0]))

    def _read_job(
        self, cursor: MySQLCursorAbstract, job_id: str, *, for_update: bool = False
    ) -> Job | None:
        """读作业快照。`for_update=True` 用锁定读拿**最新已提交**版本。

        为什么需要这个开关：REPEATABLE READ 下普通一致性读看的是事务开始时的快照，
        并发刚提交的状态它看不见。`transition` 刚做完条件更新，若再用普通读取返回值，
        可能把一个已过期的状态当结果交给调用方。
        """
        cursor.execute(f"{_SELECT_JOB} FOR UPDATE" if for_update else _SELECT_JOB, (job_id,))
        row = self._fetch_row(cursor)
        if row is None:
            return None
        return _row_to_job(row, self._read_events(cursor, job_id))

    @staticmethod
    def _read_events(cursor: MySQLCursorAbstract, job_id: str) -> tuple[Event, ...]:
        cursor.execute(_SELECT_EVENTS, (job_id,))
        return tuple(
            Event(timestamp=_from_db(row[0]), data=str(row[1]))
            for row in MySQLJobRepository._fetch_rows(cursor)
        )

    @staticmethod
    def _raise_if_blocked(cursor: MySQLCursorAbstract, job_id: str, to: JobStatus) -> None:
        """条件 UPDATE 一行都没匹配到时，判定到底发生了什么。

        只有三种可能，必须分清 —— 把「幂等重写」误判成并发冲突会让 Worker 重投递直接失败：

        - 作业已被删除 → `JobNotFoundError`
        - 状态已是终态（另一个 Worker 抢先收尾）→ `IllegalTransitionError`
        - 行还在、也还是非终态，只是值没变 → 幂等成功，什么都不做

        `FOR UPDATE` 不是装饰：这里要的是**最新已提交**的状态。第一层判定用的是普通
        一致性读，事务快照停留在并发者提交之前，直接重读会看到过期状态而漏判。
        """
        cursor.execute("SELECT status FROM jobs WHERE job_id=%s FOR UPDATE", (job_id,))
        row = MySQLJobRepository._fetch_row(cursor)
        if row is None:
            raise JobNotFoundError("作业不存在，无法迁移状态", context={"job_id": job_id})

        actual = JobStatus(str(row[0]))
        if is_terminal(actual):
            raise IllegalTransitionError(
                "作业已成终态，拒绝写入",
                context={
                    "job_id": job_id,
                    "from": actual.value,
                    "to": to.value,
                    "guard": "conditional_update",
                },
            )
        logger.debug("幂等重写未产生变更：job_id=%s status=%s", job_id, actual.value)

    def _unavailable(
        self, exc: mysql.connector.Error, operation: str, **context: Any
    ) -> RepositoryUnavailableError:
        """驱动异常 → 领域异常。日志只留操作名与 errno：连接参数里有口令，绝不整串打印。"""
        logger.error("MySQL 操作失败：operation=%s errno=%s", operation, exc.errno)
        return RepositoryUnavailableError(
            f"存储不可用（{operation}）",
            context={"operation": operation, "errno": exc.errno, **context},
        )


def _to_db(moment: datetime) -> datetime:
    """写库前统一成 UTC 再剥掉时区（列类型 `DATETIME(6)` 不存时区信息）。

    naive 输入一律**当作 UTC**并记一条警告，绝不「按本地时区猜」——
    源项目的整体偏移缺陷正是从「本地时间的 naive datetime」长出来的。
    """
    if moment.tzinfo is None:
        logger.warning("收到无时区的时间戳，按 UTC 处理：%r", moment)
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


def _from_db(value: datetime) -> datetime:
    """读出来后重新挂上 UTC。不挂时区的话，调用方拿到的是「不知道哪个时区」的时间。"""
    return value.replace(tzinfo=UTC)


def _row_to_job(row: Any, events: tuple[Event, ...]) -> Job:
    """行 → 领域对象。列顺序与上面几条 SELECT 的选择列一致。

    `row` 收成 `Any`：驱动把行标成「一长串 union（tuple | dict）」，
    展开写没有可读性；边界就在这里，逐列显式转换，转换完就是强类型的 `Job`。
    """
    return Job(
        job_id=str(row[0]),
        status=JobStatus(str(row[1])),
        customer_domain=str(row[2]),
        project_description=str(row[3]),
        result=None if row[4] is None else str(row[4]),
        created_at=_from_db(row[5]),
        updated_at=_from_db(row[6]),
        events=events,
    )
