"""模型供应商适配（spec/021 §4）。

三件事值得说明：

1. **配置从 `Settings` 读，不读环境变量。** 业务代码里出现 `os.getenv` 就没法在测试里
   显式构造配置，也没法一眼看出一个模块到底依赖哪些配置项。
2. **缺变量时报「变量名」，不报「变量值」。** 报错信息会被写进日志甚至返回给调用方，
   带上密钥就等于泄露（REQ-009）。
3. **`fake` 不在这里构造模型。** 它是离线确定性路径，替换的是**执行器**
   （`worker/bootstrap.py` 会选 `FakeFlowRunner`）。原因见 spec/021 §4.1：
   CrewAI 的 `LLM` 会把 `call` 委托给 provider 实现，桩对象拦不住，
   硬凑一个「假模型」只是在测桩本身。
"""

from __future__ import annotations

import logging
from typing import Any

from amr.config import Settings
from amr.domain.errors import InvalidInputError
from amr.domain.inputs import CrewInputs  # noqa: F401 —— 供调用方按需导入，保持适配层内聚

__all__ = ["SettingsLLMProvider", "build_llm_provider"]

logger = logging.getLogger(__name__)

#: 供应商 → 必填的 (Settings 字段名, 环境变量名)。环境变量名用于报错信息。
_REQUIRED: dict[str, tuple[tuple[str, str], ...]] = {
    "openai": (
        ("openai_api_base", "OPENAI_API_BASE"),
        ("openai_api_key", "OPENAI_API_KEY"),
        ("openai_model_name", "OPENAI_MODEL_NAME"),
    ),
    "oneapi": (("oneapi_api_key", "ONEAPI_API_KEY"),),
    "ollama": (),
}

_FAKE_HINT = (
    "AMR_LLM_PROVIDER=fake 不构造模型，它把执行器换成 FakeFlowRunner（离线确定性路径）。"
    "若要跑真实推理，请把 AMR_LLM_PROVIDER 设为 openai / oneapi / ollama。"
)


class SettingsLLMProvider:
    """按 `Settings.llm_provider` 构造 CrewAI 模型。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def name(self) -> str:
        return self._settings.llm_provider

    def build(self) -> Any:
        """构造 `crewai.LLM` 实例。"""
        if self.name == "fake":
            raise InvalidInputError(_FAKE_HINT, context={"provider": self.name})

        self._require_fields(self.name)
        if self.name == "oneapi":
            return self._build_oneapi()
        if self.name == "ollama":
            return self._build_ollama()
        return self._build_openai()

    # ------------------------------------------------------------ 各供应商

    def _build_openai(self) -> Any:
        from crewai import LLM

        s = self._settings
        logger.info("使用 OpenAI 兼容网关 provider=openai model=%s", s.openai_model_name)
        return LLM(base_url=s.openai_api_base, api_key=s.openai_api_key, model=s.openai_model_name)

    def _build_oneapi(self) -> Any:
        from crewai import LLM

        s = self._settings
        logger.info("使用 OneAPI 网关 provider=oneapi model=%s", s.oneapi_model_name)
        return LLM(
            base_url=s.oneapi_api_base,
            api_key=s.oneapi_api_key,
            model=s.oneapi_model_name,
            temperature=0.7,
        )

    def _build_ollama(self) -> Any:
        from crewai import LLM

        s = self._settings
        # OPENAI_API_KEY 占位值由 config.export_third_party_env 在装配点统一处理，
        # 适配器不再有全局副作用（spec/000 §5）
        logger.info("使用本地 Ollama provider=ollama model=%s", s.ollama_model_name)
        return LLM(
            base_url=s.ollama_api_base,
            api_key=s.ollama_api_key,
            model=s.ollama_model_name,
            temperature=0.7,
        )

    # ------------------------------------------------------------ 校验

    def _require_fields(self, provider: str) -> None:
        missing = [
            env_name
            for field_name, env_name in _REQUIRED.get(provider, ())
            if not getattr(self._settings, field_name)
        ]
        if missing:
            # 只点名变量名，绝不回显其值（REQ-009）
            raise InvalidInputError(
                f"供应商 {provider} 缺少必填配置：{', '.join(missing)}",
                context={"provider": provider, "missing": missing},
            )


def build_llm_provider(settings: Settings) -> SettingsLLMProvider:
    """工厂入口。"""
    return SettingsLLMProvider(settings)
