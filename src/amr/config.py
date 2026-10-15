"""配置的唯一入口（spec/000 §5）。

业务代码**不得**直接 `os.getenv` / `os.environ[...]`，一律经由这里的 `Settings`。
这样配置项有名字、有类型、有默认值，测试里可以显式构造而不用污染环境变量。

`.env` 用**绝对路径**定位，因此从任意工作目录启动 API / Worker 都能读到（REQ-009 / NFR-006）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["LLMProviderName", "Settings", "PROJECT_ROOT", "get_settings"]

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
        }


def get_settings() -> Settings:
    """读取配置。测试里请直接构造 `Settings(...)`，不要依赖真实 `.env`。"""
    return Settings()
