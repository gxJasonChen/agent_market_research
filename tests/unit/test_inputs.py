"""`CrewInputs` 输入校验契约测试。

守护需求：REQ-010（入参校验）。
对应规格：spec/020-contracts-domain.md §3。
用例编号：TC-U-005 ~ TC-U-006。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from amr.domain.inputs import CrewInputs

VALID_DOMAIN = "openai.com"
VALID_DESC = "为一家 AI 基础设施公司制定新品发布营销方案"


def _build(**overrides: str) -> CrewInputs:
    payload = {"customer_domain": VALID_DOMAIN, "project_description": VALID_DESC}
    payload.update(overrides)
    return CrewInputs(**payload)


def test_accepts_valid_payload() -> None:
    """TC-U-005 基线：合法入参可构造（REQ-010）。"""
    inputs = _build()
    assert inputs.customer_domain == VALID_DOMAIN
    assert inputs.project_description == VALID_DESC


@pytest.mark.parametrize("domain", ["abc", "a" * 255])
def test_customer_domain_length_boundaries_pass(domain: str) -> None:
    """TC-U-005 边界：长度 3 与 255 合法（REQ-010）。"""
    assert _build(customer_domain=domain).customer_domain == domain


@pytest.mark.parametrize("domain", ["ab", "a" * 256, ""])
def test_customer_domain_length_boundaries_fail(domain: str) -> None:
    """TC-U-005 边界：长度 2 / 256 / 空串非法（REQ-010）。"""
    with pytest.raises(ValidationError) as exc:
        _build(customer_domain=domain)
    assert "customer_domain" in str(exc.value)


@pytest.mark.parametrize("desc", ["a" * 10, "b" * 4000])
def test_project_description_length_boundaries_pass(desc: str) -> None:
    """TC-U-005 边界：长度 10 与 4000 合法（REQ-010）。"""
    assert _build(project_description=desc).project_description == desc


@pytest.mark.parametrize("desc", ["a" * 9, "b" * 4001, ""])
def test_project_description_length_boundaries_fail(desc: str) -> None:
    """TC-U-005 边界：长度 9 / 4001 / 空串非法（REQ-010）。"""
    with pytest.raises(ValidationError) as exc:
        _build(project_description=desc)
    assert "project_description" in str(exc.value)


def test_strips_surrounding_whitespace() -> None:
    """TC-U-006 首尾空白被去除后再判长度（REQ-010）。"""
    inputs = _build(customer_domain="  openai.com  ", project_description=f"  {VALID_DESC}  ")
    assert inputs.customer_domain == "openai.com"
    assert inputs.project_description == VALID_DESC


@pytest.mark.parametrize("blank", ["   ", "\t\n ", " 　 "])
def test_whitespace_only_is_rejected(blank: str) -> None:
    """TC-U-006 纯空白视为空串，必须拒绝（REQ-010）。"""
    with pytest.raises(ValidationError):
        _build(customer_domain=blank)
    with pytest.raises(ValidationError):
        _build(project_description=blank)


def test_strip_before_length_check() -> None:
    """TC-U-006 先 strip 再判长度：`"  ab  "` strip 后只有 2 字符，应拒绝（REQ-010）。"""
    with pytest.raises(ValidationError):
        _build(customer_domain="  ab  ")


def test_missing_field_is_rejected() -> None:
    """TC-U-005 补充：缺字段非法（REQ-010）。"""
    with pytest.raises(ValidationError):
        CrewInputs(customer_domain=VALID_DOMAIN)  # type: ignore[call-arg]


def test_extra_field_is_rejected() -> None:
    """TC-U-005 补充：未知字段非法，避免把拼错的字段静默吞掉（REQ-010）。"""
    with pytest.raises(ValidationError):
        CrewInputs(customer_domain=VALID_DOMAIN, project_description=VALID_DESC, typo=1)  # type: ignore[call-arg]
