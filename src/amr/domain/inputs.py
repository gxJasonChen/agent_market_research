"""执行入参与其校验规则（spec/020-contracts-domain.md §3）。

`CrewInputs` 是输入校验的**单一事实来源**：HTTP 请求体直接用它，
CrewAI 的 `kickoff(inputs=...)` 也用它，不在路由或 Crew 里另写一套规则。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["CrewInputs"]

DOMAIN_MIN_LENGTH = 3
DOMAIN_MAX_LENGTH = 255
DESCRIPTION_MIN_LENGTH = 10
DESCRIPTION_MAX_LENGTH = 4000


class CrewInputs(BaseModel):
    """一次市场调研作业的输入。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    customer_domain: str = Field(
        min_length=DOMAIN_MIN_LENGTH,
        max_length=DOMAIN_MAX_LENGTH,
        description="客户领域 / 官网，作为调研起点",
    )
    project_description: str = Field(
        min_length=DESCRIPTION_MIN_LENGTH,
        max_length=DESCRIPTION_MAX_LENGTH,
        description="项目背景与目标",
    )

    @field_validator("customer_domain", "project_description", mode="before")
    @classmethod
    def _strip_whitespace(cls, value: object) -> object:
        """先 strip 再判长度：`"  ab  "` 应当按 2 个字符处理（REQ-010）。"""
        if isinstance(value, str):
            return value.strip()
        return value
