"""配置模块（Codex review L4 / M3 / L13）。

守护需求：REQ-009（配置安全）、REQ-013（可观测性）。

这些用例守的是「**文档让用户设了、但代码从没读过**」这类死配置——
死配置比没有配置更糟：它会让人以为自己已经配好了。

用例编号：TC-U-028。
"""

from __future__ import annotations

import logging
import os

import pytest

from amr.config import Settings, configure_logging, export_third_party_env

SENTINEL = "sk-super-secret-value"


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


# ------------------------------------------------------------------ TC-U-028 日志


def test_log_level_actually_applied() -> None:
    """TC-U-028 `AMR_LOG_LEVEL` 必须真的生效（REQ-013）。

    在此之前它是个没人读的字段：文档让用户设，设了没有效果。
    """
    configure_logging(_settings(log_level="WARNING"))

    assert logging.getLogger().level == logging.WARNING
    configure_logging(_settings(log_level="INFO"))  # 复位，避免影响其它用例


def test_log_level_is_case_insensitive() -> None:
    """TC-U-028 补充：小写 `debug` 也要认（人写配置不会记得大写）（REQ-013）。"""
    configure_logging(_settings(log_level="debug"))

    assert logging.getLogger().level == logging.DEBUG
    configure_logging(_settings(log_level="INFO"))


# ------------------------------------------------------------------ TC-U-028 环境导出


def test_tracing_flag_is_exported_to_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """TC-U-028 CrewAI Traces 开关必须真的导出到进程环境（REQ-013）。

    关键点：pydantic-settings 只把 `.env` 读进 `Settings` 对象，**不会**写进 `os.environ`；
    而 crewai 是从进程环境读这个开关的。不导出的话，用户按文档设了也毫无效果。
    """
    monkeypatch.delenv("CREWAI_TRACING_ENABLED", raising=False)

    export_third_party_env(_settings(crewai_tracing_enabled=True))

    assert os.environ["CREWAI_TRACING_ENABLED"] == "true"


def test_tracing_flag_not_exported_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """TC-U-028 补充：关掉时不要往环境里塞东西（REQ-013）。"""
    monkeypatch.delenv("CREWAI_TRACING_ENABLED", raising=False)

    export_third_party_env(_settings(crewai_tracing_enabled=False))

    assert "CREWAI_TRACING_ENABLED" not in os.environ


def test_ollama_gets_openai_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    """TC-U-028 本地推理没有真 key，需要给 crewai 一个占位值（REQ-008）。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    export_third_party_env(_settings(llm_provider="ollama"))

    assert os.environ["OPENAI_API_KEY"] == "NA"


def test_ollama_placeholder_does_not_override_real_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """TC-U-028 用 `setdefault`，不覆盖用户显式配置的真实 key（REQ-008）。"""
    monkeypatch.setenv("OPENAI_API_KEY", "user-real-key")

    export_third_party_env(_settings(llm_provider="ollama"))

    assert os.environ["OPENAI_API_KEY"] == "user-real-key"


def test_non_ollama_provider_leaves_env_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """TC-U-028 补充：非 ollama 供应商不该动环境（REQ-008）。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    export_third_party_env(_settings(llm_provider="openai"))

    assert "OPENAI_API_KEY" not in os.environ


# ------------------------------------------------------------------ TC-U-028 Redis 地址


@pytest.mark.parametrize(
    ("password", "expected_auth"),
    [
        ("", ""),
        ("simple", ":simple@"),
        ("p@ss:word/with#chars", ":p%40ss%3Aword%2Fwith%23chars@"),
    ],
)
def test_redis_broker_url_encodes_password(password: str, expected_auth: str) -> None:
    """TC-U-028 Redis 口令里的特殊字符自动 URL 编码（REQ-009）。

    源项目踩过这个坑：口令里的 `@` `:` `/` 不编码会让 broker URL 解析成别的主机，
    表现为「连不上队列」但看不出原因。手工转义又容易出错，所以由代码统一做。
    """
    settings = _settings(
        redis_host="cache.internal", redis_port=6380, redis_db=2, redis_password=password
    )

    assert settings.redis_broker_url == f"redis://{expected_auth}cache.internal:6380/2"


def test_mysql_config_has_explicit_timeouts() -> None:
    """TC-U-028 数据库连接必须有超时（Codex review L17）。

    没有超时时，被黑洞的数据库会让请求一直挂着；而路由是同步 `def`（跑在线程池里），
    少量挂起就能把线程池占满，提交与查询一起变慢。
    """
    config = _settings().mysql_config

    assert config["connect_timeout"] > 0
    assert config["read_timeout"] > 0
    assert config["write_timeout"] > 0


def test_settings_never_expose_password_in_repr() -> None:
    """TC-U-028 补充：`repr(Settings)` 会带出口令——不要在日志里直接打它（REQ-009）。"""
    settings = _settings(mysql_password=SENTINEL)

    # 这条不是要求 repr 隐藏（那不现实），而是把「已知风险」钉成事实：
    # 一旦有人想 log settings，必须先来做脱敏，测试会提醒他这里确实会泄露
    assert SENTINEL in repr(settings)
