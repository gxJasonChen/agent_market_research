"""领域模型契约测试。

守护需求：REQ-006（结构化输出）。
对应规格：spec/020-contracts-domain.md §2。
用例编号：TC-U-007 ~ TC-U-008。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from amr.domain.errors import SchemaValidationError
from amr.domain.models import CampaignIdea, Copy, FlowResult, MarketStrategy

STRATEGY = MarketStrategy(
    name="开发者心智占领",
    tactics=["开源 SDK", "技术博客"],
    channels=["GitHub", "掘金"],
    kpis=["Star 增速", "注册转化率"],
)
IDEA = CampaignIdea(
    name="48 小时黑客松",
    description="面向独立开发者的线上黑客松",
    audience="独立开发者",
    channel="线上社区",
)
COPY = Copy(title="把模型成本打下来", body="三行代码接入，成本立省 60%。")


def _result() -> FlowResult:
    return FlowResult(
        research_report="调研结论……", strategy=STRATEGY, campaign_idea=IDEA, copy=COPY
    )


def test_flow_result_json_round_trip() -> None:
    """TC-U-007 结果序列化后能无损反序列化（REQ-006）。"""
    original = _result()
    restored = FlowResult.from_json(original.to_json())
    assert restored == original


def test_to_json_shape_matches_contract() -> None:
    """TC-U-007 补充：JSON 形状与 spec/020 §2.3 完全一致（REQ-006 / CT-002）。"""
    payload = json.loads(_result().to_json())
    assert set(payload) == {"research_report", "strategy", "campaign_idea", "copy"}
    assert payload["strategy"] == {
        "name": "开发者心智占领",
        "tactics": ["开源 SDK", "技术博客"],
        "channels": ["GitHub", "掘金"],
        "kpis": ["Star 增速", "注册转化率"],
    }
    assert payload["copy"] == {"title": "把模型成本打下来", "body": "三行代码接入，成本立省 60%。"}


def test_from_json_accepts_dict_payload() -> None:
    """TC-U-007 补充：`from_json` 既能吃字符串也能吃已解析的对象（REQ-006）。"""
    payload = json.loads(_result().to_json())
    assert FlowResult.from_json(payload) == _result()


@pytest.mark.parametrize(
    "broken",
    [
        {"research_report": "x", "strategy": {"name": "n"}, "campaign_idea": {}, "copy": {}},
        {"research_report": "x", "copy": {"title": "t", "body": "b"}},
        {"research_report": "x", "strategy": None, "campaign_idea": None, "copy": None},
    ],
)
def test_from_json_rejects_incomplete_payload(broken: dict[str, object]) -> None:
    """TC-U-008 字段缺失 / 为 null → SchemaValidationError（REQ-006）。"""
    with pytest.raises(SchemaValidationError):
        FlowResult.from_json(broken)


def test_from_json_error_message_names_the_field() -> None:
    """TC-U-008 错误信息必须点名是哪个字段坏了，便于排障（REQ-006）。"""
    broken = {
        "research_report": "x",
        "strategy": {"name": "n", "tactics": "不是数组", "channels": [], "kpis": []},
        "campaign_idea": {"name": "n", "description": "d", "audience": "a", "channel": "c"},
        "copy": {"title": "t", "body": "b"},
    }
    with pytest.raises(SchemaValidationError) as exc:
        FlowResult.from_json(broken)
    assert "strategy" in str(exc.value)
    assert "tactics" in str(exc.value)


def test_from_json_rejects_malformed_json_text() -> None:
    """TC-U-008 非 JSON 文本 → SchemaValidationError，而不是裸 JSONDecodeError（REQ-006）。"""
    with pytest.raises(SchemaValidationError):
        FlowResult.from_json("{不是 JSON")


def test_market_strategy_rejects_legacy_field_names() -> None:
    """TC-U-008 补充：源项目的 `tatics` / `KPIs` 已废弃，不做别名兼容（REQ-006，差异项 9）。"""
    with pytest.raises(ValidationError):
        MarketStrategy(name="n", tatics=["a"], channels=[], KPIs=[])  # type: ignore[call-arg]


def test_models_are_frozen_value_objects() -> None:
    """TC-U-007 补充：模型不可变，避免共享实例被下游就地改写（REQ-006）。"""
    with pytest.raises(ValidationError):
        STRATEGY.name = "改名"  # type: ignore[misc]
