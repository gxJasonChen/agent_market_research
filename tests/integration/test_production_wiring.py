"""生产装配路径的集成用例（Codex review M5）。

守护需求：REQ-009、REQ-014。

**为什么需要它**：`create_app()` **无参**调用（uvicorn 的真实入口）在门禁里从未被执行过——
集成测试全都注入内存实现，于是「生产装配」这段唯一可能把 MySQL / Redis / celery
接错的地方，恰恰没人跑过。本文件补上这条，且**不触网、不连库**：
只构造，不调用仓储方法。

用例编号：TC-I-014。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from amr.adapters.celery_queue import CeleryTaskQueue
from amr.adapters.mysql_repository import MySQLJobRepository
from amr.adapters.system_clock import SystemClock, Uuid4Generator
from amr.api.app import create_app
from amr.config import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


# ------------------------------------------------------------------ TC-I-014


def test_create_app_without_arguments_builds_production_dependencies() -> None:
    """TC-I-014 `create_app()` 无参调用能装配出生产实现，且**不需要**外部系统（REQ-009）。

    构造仓储不连库、构造 Celery 不连 broker——这正是「延迟到首次使用」的意义。
    """
    settings = _settings(mysql_host="192.0.2.1", redis_host="192.0.2.1")

    application = create_app(settings)

    dependencies = application.state.dependencies.get()
    assert isinstance(dependencies.repository, MySQLJobRepository)
    assert isinstance(dependencies.queue, CeleryTaskQueue)
    assert isinstance(dependencies.clock, SystemClock)
    assert isinstance(dependencies.ids, Uuid4Generator)


def test_production_app_serves_healthz_without_touching_dependencies() -> None:
    """TC-I-014 `/healthz` 在**完全没有中间件**的情况下也必须可用（REQ-014）。

    探活不该依赖下游：数据库挂了不代表进程该被重启。这里的地址是 TEST-NET-1
    （RFC 5737 保留给文档用，永远不可达），能返回 200 才说明真的没去连。
    """
    app = create_app(_settings(mysql_host="192.0.2.1", redis_host="192.0.2.1"))

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_production_app_is_import_safe() -> None:
    """TC-I-014 模块 import 不得触发配置读取（Codex review L18）（REQ-009）。

    `import amr.api.app` 必须是「无害」的：`.env` 里任何一个值写错，
    都不该让 import 本身炸掉——那会波及测试收集、文档工具，以及任何一次误 import。
    """
    import importlib

    module = importlib.import_module("amr.api.app")
    importlib.reload(module)

    assert not hasattr(module, "app"), (
        "模块级不该有 app 实例：ASGI 入口已改为工厂（uvicorn ...:create_app --factory）"
    )


@pytest.mark.parametrize("path", ["/api/crew/does-not-exist"])
def test_production_app_reports_storage_failure_as_503(path: str) -> None:
    """TC-I-014 存储不可用时，查询接口返回 503 而不是 500（REQ-011）。

    用不可达地址触发真实的连接失败路径——这条链路上「驱动异常 → 领域异常 → 状态码」
    三段翻译以前没有任何用例覆盖。
    """
    app = create_app(_settings(mysql_host="192.0.2.1", mysql_port=3306))

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(path)

    assert response.status_code == 503
    assert response.json() == {"detail": "存储不可用"}
