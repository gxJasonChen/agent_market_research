"""模型供应商适配的单元测试。

守护需求：REQ-008（多供应商）、REQ-009（配置安全）。
用例编号：TC-U-020 ~ TC-U-021。
"""

from __future__ import annotations

import pytest

from amr.adapters.llm_factory import SettingsLLMProvider, build_llm_provider
from amr.config import Settings
from amr.domain.errors import InvalidInputError


def _settings(**overrides: object) -> Settings:
    """显式构造配置：不读 `.env`，用例之间不互相污染。"""
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def _with_secret(secret: str = "sk-super-secret-value") -> Settings:
    return _settings(
        llm_provider="openai",
        openai_api_base="https://gateway.example.com/v1",
        openai_api_key=secret,
        openai_model_name="gpt-4o-mini",
    )


# ------------------------------------------------------------------ TC-U-020


@pytest.mark.parametrize(
    ("overrides", "missing_var"),
    [
        (
            {"openai_api_base": "", "openai_api_key": "k", "openai_model_name": "m"},
            "OPENAI_API_BASE",
        ),
        (
            {"openai_api_base": "u", "openai_api_key": "", "openai_model_name": "m"},
            "OPENAI_API_KEY",
        ),
        (
            {"openai_api_base": "u", "openai_api_key": "k", "openai_model_name": ""},
            "OPENAI_MODEL_NAME",
        ),
    ],
)
def test_missing_variable_error_names_the_variable(
    overrides: dict[str, str], missing_var: str
) -> None:
    """TC-U-020 缺配置时报**变量名**，让人知道该去填哪一项（REQ-008）。"""
    settings = _settings(llm_provider="openai", **overrides)

    with pytest.raises(InvalidInputError) as exc:
        build_llm_provider(settings).build()

    assert missing_var in str(exc.value)


def test_error_message_never_leaks_the_secret() -> None:
    """TC-U-020 错误信息里**绝不能**出现配置值（REQ-009）。

    报错会被写进日志、甚至透到调用方；带上密钥就等于泄露。
    """
    secret = "sk-super-secret-value"
    # 只缺 model name，key 是有的——最容易犯的错就是把「当前配置」整个打出来
    settings = _settings(
        llm_provider="openai",
        openai_api_base="https://gateway.example.com/v1",
        openai_api_key=secret,
        openai_model_name="",
    )

    with pytest.raises(InvalidInputError) as exc:
        build_llm_provider(settings).build()

    assert secret not in str(exc.value)
    assert secret not in repr(exc.value.context)


def test_oneapi_missing_key_names_it() -> None:
    """TC-U-020 补充：oneapi 只强制要求密钥（REQ-008）。"""
    settings = _settings(llm_provider="oneapi", oneapi_api_key="")

    with pytest.raises(InvalidInputError) as exc:
        build_llm_provider(settings).build()

    assert "ONEAPI_API_KEY" in str(exc.value)


# ------------------------------------------------------------------ TC-U-021


def test_openai_provider_builds_a_model() -> None:
    """TC-U-021 openai 分支可以构造出模型对象（REQ-008）。"""
    model = build_llm_provider(_with_secret()).build()

    assert model is not None
    assert model.model == "gpt-4o-mini"


def test_oneapi_provider_builds_a_model() -> None:
    """TC-U-021 oneapi 分支可以构造出模型对象（REQ-008）。"""
    settings = _settings(
        llm_provider="oneapi",
        oneapi_api_base="http://gateway.local:3000/v1",
        oneapi_api_key="oneapi-key",
        oneapi_model_name="qwen-max",
    )

    assert build_llm_provider(settings).build().model == "qwen-max"


def test_ollama_provider_builds_a_model() -> None:
    """TC-U-021 ollama 分支可以构造出模型对象（REQ-008）。

    OPENAI_API_KEY 占位值由 `config.export_third_party_env` 在装配点统一设置，
    适配器本身不再有全局副作用（其行为由 tests/unit/test_config.py 覆盖）。
    """
    settings = _settings(llm_provider="ollama", ollama_model_name="llama3.1:latest")

    assert build_llm_provider(settings).build().model == "llama3.1:latest"


def test_llm_factory_has_no_global_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """TC-U-021 适配器不得改动进程环境（spec/000 §5）。

    这类副作用搬去装配点后，适配层才谈得上「无状态、可反复构造」。
    """
    import os

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    build_llm_provider(_settings(llm_provider="ollama")).build()

    assert "OPENAI_API_KEY" not in os.environ


def test_fake_provider_refuses_to_build_a_model() -> None:
    """TC-U-021 `fake` 不构造模型，而是指向执行器选择（spec/021 §4.1）。

    CrewAI 的 `LLM` 会把 `call` 委托给 provider 实现，桩对象拦不住；
    与其造一个「假模型」让测试去测桩本身，不如在装配层明确换成 FakeFlowRunner。
    """
    with pytest.raises(InvalidInputError) as exc:
        build_llm_provider(_settings(llm_provider="fake")).build()

    message = str(exc.value)
    assert "FakeFlowRunner" in message
    assert "openai" in message  # 告诉使用者该改成什么


def test_provider_name_is_exposed() -> None:
    """TC-U-021 补充：供应商名字可读，便于日志与排障（REQ-008）。"""
    assert SettingsLLMProvider(_settings(llm_provider="ollama")).name == "ollama"
