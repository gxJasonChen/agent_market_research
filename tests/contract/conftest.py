"""契约层夹具：同一套用例跑过 `JobRepository` 的每个实现（spec/021 §1.1、spec/040 §2.2）。

两条硬约束：

- **离线可跑**（NFR-001）：没有 MySQL 的机器上 `mysql` 参数整体 `skip`，不让 CI 变红。
  skip 只允许发生在**建库阶段**（连不上 / 没权限）。一旦连上，后面任何失败都是真失败 ——
  用宽泛的 except 把 SQL 写错也吞成 skip，契约层就形同虚设。
- **不碰生产库**：只在 `Settings().mysql_database + "_test"` 上读写，每个用例前清空两张表。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator

import mysql.connector
import pytest

from amr.adapters.memory_repository import MemoryJobRepository
from amr.adapters.mysql_repository import MySQLJobRepository
from amr.config import Settings
from amr.ports.repository import JobRepository

__all__ = ["TEST_DATABASE_SUFFIX", "mysql_clone_factory", "mysql_repository", "repository"]

#: 测试库后缀：契约测试永远不碰生产库。
TEST_DATABASE_SUFFIX = "_test"

#: 库名要拼进 CREATE DATABASE —— 标识符没法参数化，所以白名单校验后再拼（spec/000 §7）。
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+")


def _test_settings() -> Settings:
    """测试库配置：只把库名换成 `<生产库名>_test`，其余连接参数照旧由 `Settings` 决定。"""
    settings = Settings()
    return settings.model_copy(
        update={"mysql_database": settings.mysql_database + TEST_DATABASE_SUFFIX}
    )


def _connect(settings: Settings, *, with_database: bool = True):
    """开一条独立连接。`with_database=False` 用于建库前（库还不存在）。"""
    config: dict[str, object] = dict(settings.mysql_config)
    if not with_database:
        config.pop("database", None)
    return mysql.connector.connect(**config)


def _create_database(settings: Settings) -> None:
    """`CREATE DATABASE IF NOT EXISTS`。这里是**唯一**会被 skip 吞掉的失败点。"""
    name = settings.mysql_database
    if not _SAFE_IDENTIFIER.fullmatch(name):
        raise ValueError(f"测试库名只允许字母、数字与下划线：{name!r}")

    connection = _connect(settings, with_database=False)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS {name} CHARACTER SET utf8mb4")
        connection.commit()
    finally:
        connection.close()


def _reset_tables(settings: Settings) -> None:
    """清空两张表。**先 events 后 jobs** —— `events` 有指向 `jobs` 的外键，反过来删不掉。"""
    connection = _connect(settings)
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM events")
            cursor.execute("DELETE FROM jobs")
        connection.commit()
    finally:
        connection.close()


def _prepare_database() -> Settings:
    """建测试库并返回配置。MySQL 不可用（连不上 / 无权限）时跳过本参数。"""
    settings = _test_settings()
    try:
        _create_database(settings)
    except mysql.connector.Error as exc:
        pytest.skip(
            f"MySQL 不可用，跳过契约测试的 MySQL 半边：{type(exc).__name__} errno={exc.errno}"
        )
    return settings


def _fresh_mysql_repository() -> MySQLJobRepository:
    """建库 → 建表 → 清表，返回可直接使用的实例（MySQL 不可用则 skip）。"""
    settings = _prepare_database()
    repository = MySQLJobRepository(settings)
    repository.ensure_schema()
    _reset_tables(settings)
    return repository


@pytest.fixture(params=("memory", "mysql"))
def repository(request: pytest.FixtureRequest) -> Iterator[JobRepository]:
    """契约测试的主角：内存实现与 MySQL 实现跑**同一套断言**。"""
    if request.param == "memory":
        yield MemoryJobRepository()
        return
    yield _fresh_mysql_repository()


@pytest.fixture
def mysql_repository() -> MySQLJobRepository:
    """只要 MySQL 实现的用例（并发语义在这里才验得出来）。"""
    return _fresh_mysql_repository()


@pytest.fixture
def mysql_clone_factory() -> Callable[[], MySQLJobRepository]:
    """返回「另一个 Worker 进程」视角的仓储工厂：每次调用都是一条独立连接。

    表已建好、数据已清空，工厂本身不再清表 —— 用例可以在同一个数据库上持有多条连接。
    """
    settings = _prepare_database()
    seed = MySQLJobRepository(settings)
    seed.ensure_schema()
    _reset_tables(settings)
    return lambda: MySQLJobRepository(settings)
