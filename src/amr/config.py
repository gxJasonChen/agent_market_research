"""配置的唯一入口（spec/000 §5）。

业务代码**不得**直接 `os.getenv` / `os.environ[...]`，一律经由这里的 `Settings`。
这样配置项有名字、有类型、有默认值，测试里可以显式构造而不用污染环境变量。

`.env` 用**绝对路径**定位，因此从任意工作目录启动 API / Worker 都能读到（REQ-009 / NFR-006）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "PROJECT_ROOT",
    "LLMProviderName",
    "Settings",
    "configure_logging",
    "export_third_party_env",
    "get_settings",
]

#: 仓库根目录（src/amr/config.py → parents[2]）
PROJECT_ROOT = Path(__file__).resolve().parents[2]

LLMProviderName = Literal["openai", "oneapi", "ollama", "fake"]


class Settings(BaseSettings):
    """进程配置。字段名即代码里的引用名，`validation_alias` 即环境变量名。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # 允许按**字段名**构造：测试写 `Settings(_env_file=None, llm_provider="openai")`
        # 就能显式给配置，不必去动环境变量——环境变量是全局状态，会让用例互相污染。
        populate_by_name=True,
    )

    # ---------------------------------------------------------- 模型供应商
    llm_provider: LLMProviderName = Field(default="openai", validation_alias="AMR_LLM_PROVIDER")

    openai_api_base: str = Field(default="", validation_alias="OPENAI_API_BASE")
    openai_api_key: str = Field(default="", validation_alias="OPENAI_API_KEY")
    openai_model_name: str = Field(default="", validation_alias="OPENAI_MODEL_NAME")

    oneapi_api_base: str = Field(
        default="http://localhost:3000/v1", validation_alias="ONEAPI_API_BASE"
    )
    oneapi_api_key: str = Field(default="", validation_alias="ONEAPI_API_KEY")
    oneapi_model_name: str = Field(default="qwen-max", validation_alias="ONEAPI_MODEL_NAME")

    ollama_api_base: str = Field(
        default="http://localhost:11434/v1", validation_alias="OLLAMA_API_BASE"
    )
    ollama_api_key: str = Field(default="ollama", validation_alias="OLLAMA_API_KEY")
    ollama_model_name: str = Field(default="llama3.1:latest", validation_alias="OLLAMA_MODEL_NAME")

    # ---------------------------------------------------------- 检索工具
    serper_api_key: str = Field(default="", validation_alias="SERPER_API_KEY")

    # ---------------------------------------------------------- MySQL
    mysql_host: str = Field(default="127.0.0.1", validation_alias="MYSQL_HOST")
    mysql_port: int = Field(default=3306, validation_alias="MYSQL_PORT")
    mysql_user: str = Field(default="root", validation_alias="MYSQL_USER")
    mysql_password: str = Field(default="", validation_alias="MYSQL_PASSWORD")
    mysql_database: str = Field(default="amr", validation_alias="MYSQL_DATABASE")

    # ---------------------------------------------------------- Redis
    redis_host: str = Field(default="127.0.0.1", validation_alias="REDIS_HOST")
    redis_port: int = Field(default=6379, validation_alias="REDIS_PORT")
    redis_db: int = Field(default=0, validation_alias="REDIS_DB")
    redis_password: str = Field(default="", validation_alias="REDIS_PASSWORD")

    # ---------------------------------------------------------- 服务
    api_port: int = Field(default=8012, validation_alias="AMR_API_PORT")
    # 逗号分隔。默认**不是** `*`：`*` 与 allow_credentials 同用是无效且不安全的组合（CT-004）
    cors_origins_raw: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        validation_alias="AMR_CORS_ORIGINS",
    )
    log_level: str = Field(default="INFO", validation_alias="AMR_LOG_LEVEL")
    crewai_tracing_enabled: bool = Field(default=False, validation_alias="CREWAI_TRACING_ENABLED")

    # ---------------------------------------------------------- 派生值

    @property
    def cors_origins(self) -> list[str]:
        """允许的跨域来源列表。"""
        return [item.strip() for item in self.cors_origins_raw.split(",") if item.strip()]

    @property
    def redis_broker_url(self) -> str:
        """Celery broker 地址。

        口令里的特殊字符（`@` `:` `/`）用 `quote_plus` 编码后再拼进 URL ——
        手工转义是源项目踩过的坑（REQ-009）。
        """
        auth = f":{quote_plus(self.redis_password)}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def mysql_config(self) -> dict[str, object]:
        """`mysql.connector.connect(**...)` 的参数。"""
        return {
            "host": self.mysql_host,
            "port": self.mysql_port,
            "user": self.mysql_user,
            "password": self.mysql_password,
            "database": self.mysql_database,
            # 显式超时：DB 被黑洞（DROP 而非 REJECT）时，没有超时会让请求一直挂着，
            # 而路由是同步 def（跑在线程池里），少量挂起就能把线程池占满
            "connect_timeout": 5,
            "read_timeout": 10,
            "write_timeout": 10,
        }


def get_settings() -> Settings:
    """读取配置。测试里请直接构造 `Settings(...)`，不要依赖真实 `.env`。"""
    return Settings()


def configure_logging(settings: Settings) -> None:
    """按配置初始化根日志（REQ-013）。

    在此之前 `AMR_LOG_LEVEL` 是个没人读的字段——文档让用户去设，设了却没有效果，
    这种「死配置」比没有配置更糟。

    格式是**键值文本**而不是 JSON：这套系统的日志主要给人看（排障时 `grep job_id=`），
    真要接采集器再换成 JSON 也不影响调用方。
    """
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,  # 覆盖第三方库可能已经装上的 handler，否则配置会被无声忽略
    )


def export_third_party_env(settings: Settings) -> None:
    """把 `Settings` 里的开关导出成第三方库能读到的**进程环境变量**。

    为什么需要这一步：pydantic-settings 只把 `.env` 读进 `Settings` 对象，
    **不会**把它写进 `os.environ`；而 crewai 这类库是从进程环境变量读配置的。
    不导出的话，用户按文档在 `.env` 里设了 `CREWAI_TRACING_ENABLED=true` 也毫无效果
    （这是一处会把人带偏的「死配置」）。

    把这类「适配第三方期望」的全局副作用**集中在这一个函数**，而不是散落在
    各适配器的 `build()` 里——适配层保持无副作用，测试里也只需要显式调这一次。
    """
    if settings.crewai_tracing_enabled:
        # crewai 自己在 import/构造时读这个变量
        os.environ["CREWAI_TRACING_ENABLED"] = "true"

    if settings.llm_provider == "ollama":
        # crewai 内部仍会读 OPENAI_API_KEY；本地推理没有真 key，给个占位值避免它报错。
        # 用 setdefault 而不是赋值：不覆盖用户显式配置的真实 key。
        os.environ.setdefault("OPENAI_API_KEY", "NA")
