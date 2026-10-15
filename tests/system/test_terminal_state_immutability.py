"""终态不可变——在**真实 MySQL** 上验证（spec/040 §2.4 TC-S-002）。

契约测试（tests/contract）已经在两个实现上跑过这条规则；这里再单独做一次
系统级验证，理由很直接：这是源项目最严重缺陷的落点，而且 MySQL 实现里那句
「条件 UPDATE + affected rows 校验」只有在真实并发/真实数据库上才验得出来。
多一层独立验证不算浪费。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import mysql.connector
import pytest

from amr.adapters.mysql_repository import MySQLJobRepository
from amr.config import Settings
from amr.domain.errors import IllegalTransitionError
from amr.domain.models import Job
from amr.domain.status import JobStatus

#: 库名要拼进 CREATE DATABASE，标识符无法参数化，白名单校验后再拼
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+")


def _make_job(job_id: str, now: datetime) -> Job:
    return Job(
        job_id=job_id,
        status=JobStatus.PENDING,
        customer_domain="openai.com",
        project_description="系统测试用的项目描述，长度足够",
        result=None,
        created_at=now,
        updated_at=now,
    )


def _test_settings(settings: Settings) -> Settings:
    """指向独立测试库的配置，避免污染开发数据。

    `Settings` 是 pydantic 模型，改字段用 `model_copy`（`dataclasses.replace` 不认它）。
    """
    return settings.model_copy(update={"mysql_database": f"{settings.mysql_database}_test"})


@pytest.fixture
def test_settings(settings: Settings) -> Settings:
    """确保测试库存在，并返回指向它的配置。"""
    resolved = _test_settings(settings)
    config = dict(resolved.mysql_config)
    database = config.pop("database")

    # 建库这一步要连「服务器」而不是「某个库」，所以先摘掉 database 再连。
    # 标识符没法参数化，所以先过白名单再拼——与 tests/contract/conftest.py 同一规矩（spec/000 §7）
    if not _SAFE_IDENTIFIER.fullmatch(str(database)):
        raise ValueError(f"测试库名只允许字母、数字与下划线：{database!r}")

    conn = mysql.connector.connect(**config)  # type: ignore[arg-type]
    try:
        cursor = conn.cursor()
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS {database} CHARACTER SET utf8mb4")
        conn.commit()
    finally:
        conn.close()
    return resolved


@pytest.fixture
def repository(test_settings: Settings) -> MySQLJobRepository:
    return MySQLJobRepository(test_settings)


def _read_status_directly(test_settings: Settings, job_id: str) -> str | None:
    """绕开仓储实现，直接查库——验证「库里到底是什么」而不是「仓储说是什么」。"""
    conn = mysql.connector.connect(**test_settings.mysql_config)  # type: ignore[arg-type]
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM jobs WHERE job_id = %s", (job_id,))
        row = cursor.fetchone()
        return None if row is None else str(row[0])
    finally:
        conn.close()


def test_error_state_cannot_be_overwritten_by_complete(
    repository: MySQLJobRepository, test_settings: Settings
) -> None:
    """TC-S-002 失败作业在真实库里也不能被改写成成功（REQ-004）。"""
    job_id = "sys-terminal-immutability-1"
    now = datetime.now(UTC)
    _cleanup(test_settings, job_id)

    repository.create(_make_job(job_id, now))
    repository.transition(job_id, JobStatus.RUNNING, at=now)
    repository.transition(job_id, JobStatus.ERROR, result="调研阶段失败", at=now)

    with pytest.raises(IllegalTransitionError):
        repository.transition(job_id, JobStatus.COMPLETE, result='{"fake": "result"}', at=now)

    assert _read_status_directly(test_settings, job_id) == "ERROR"


def test_concurrent_finish_keeps_first_terminal_state(
    repository: MySQLJobRepository, test_settings: Settings
) -> None:
    """TC-S-002 补充：两个收尾并发写入时，先到的终态不被覆盖（REQ-004）。

    这是条件 UPDATE 存在的理由：只靠应用层判断会有 TOCTOU 窗口。
    """
    job_id = "sys-terminal-immutability-concurrent"
    now = datetime.now(UTC)
    _cleanup(test_settings, job_id)

    repository.create(_make_job(job_id, now))
    repository.transition(job_id, JobStatus.RUNNING, at=now)

    def finish_error() -> str:
        try:
            repository.transition(job_id, JobStatus.ERROR, result="先失败", at=now)
            return "error-ok"
        except IllegalTransitionError:
            return "error-rejected"

    def finish_complete() -> str:
        try:
            repository.transition(job_id, JobStatus.COMPLETE, result='{"x": 1}', at=now)
            return "complete-ok"
        except IllegalTransitionError:
            return "complete-rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = {pool.submit(finish_error), pool.submit(finish_complete)}
        results = [future.result() for future in outcomes]

    # 恰好一个成功、一个被拒；库里状态与成功那方一致
    assert sorted(results) in (["complete-ok", "error-rejected"], ["complete-rejected", "error-ok"])
    final = _read_status_directly(test_settings, job_id)
    assert final in {"COMPLETE", "ERROR"}
    if "error-ok" in results:
        assert final == "ERROR"
    else:
        assert final == "COMPLETE"


def _cleanup(test_settings: Settings, job_id: str) -> None:
    conn = mysql.connector.connect(**test_settings.mysql_config)  # type: ignore[arg-type]
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM events WHERE job_id = %s", (job_id,))
        cursor.execute("DELETE FROM jobs WHERE job_id = %s", (job_id,))
        conn.commit()
    finally:
        conn.close()
