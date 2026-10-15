"""CrewAI 产出 → 结构化模型的解析（Codex review M6 / L12）。

守护需求：REQ-006 —— 「模型输出必须先过 Schema 校验才能进入 result」。

为什么单独一个文件：离线用例大多用 `FakeFlowRunner`，它**直接返回 `FlowResult`**，
把「模型输出 → 校验」这一段整个绕过去了。于是 REQ-006 的后半句在相当长时间里
只有 domain 侧（`FlowResult.from_json`）被验证，真正处理 CrewAI 产出的这段没有用例。

用例编号：TC-U-027。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from amr.crews._output import output_text, structured_output
from amr.domain.errors import SchemaValidationError
from amr.domain.models import Copy, MarketStrategy

VALID_STRATEGY = {
    "name": "开发者心智占领",
    "tactics": ["开源 SDK"],
    "channels": ["GitHub"],
    "kpis": ["Star 增速"],
}


def _output(**kwargs: Any) -> Any:
    """伪造 CrewAI 的 TaskOutput：有 `raw` 与 `pydantic` 两个属性。"""
    kwargs.setdefault("raw", "")
    kwargs.setdefault("pydantic", None)
    return SimpleNamespace(**kwargs)


# ------------------------------------------------------------------ TC-U-027


def test_prefers_crewai_parsed_pydantic_object() -> None:
    """TC-U-027 第一条路径：CrewAI 已用 `output_json` 解析好，直接用（最可靠）（REQ-006）。"""
    parsed = MarketStrategy.model_validate(VALID_STRATEGY)
    output = _output(raw="（无关文本）", pydantic=parsed)

    assert structured_output(output, MarketStrategy) is parsed


def test_falls_back_to_parsing_raw_json() -> None:
    """TC-U-027 第二条路径：`pydantic` 缺失时退回解析 `raw`（REQ-006）。

    真实模型偶尔会把 JSON 包在说明文字之外单独给出来，但 `pydantic` 字段没填上；
    这条路能救回一部分。
    """
    output = _output(raw=json.dumps(VALID_STRATEGY, ensure_ascii=False), pydantic=None)

    parsed = structured_output(output, MarketStrategy)

    assert parsed.name == "开发者心智占领"
    assert parsed.tactics == ["开源 SDK"]


def test_raises_schema_error_when_both_paths_fail() -> None:
    """TC-U-027 两条路都走不通 → `SchemaValidationError`，**绝不**把裸文本当结果（REQ-006）。"""
    output = _output(raw="我认为这个战略应该聚焦开发者，但我不打算输出 JSON", pydantic=None)

    with pytest.raises(SchemaValidationError) as exc:
        structured_output(output, MarketStrategy)

    assert "MarketStrategy" in str(exc.value)


def test_schema_error_names_the_broken_field() -> None:
    """TC-U-027 错误信息要点名**模型 + 字段**，排障时不用猜（REQ-006）。"""
    broken = dict(VALID_STRATEGY, tactics="不是数组")
    output = _output(raw=json.dumps(broken, ensure_ascii=False), pydantic=None)

    with pytest.raises(SchemaValidationError) as exc:
        structured_output(output, MarketStrategy)

    assert "tactics" in str(exc.value)


def test_ignores_pydantic_object_of_the_wrong_type() -> None:
    """TC-U-027 补充：`pydantic` 里装的是别的模型时不能将就用（REQ-006）。"""
    wrong = Copy(title="标题", body="正文")
    output = _output(raw=json.dumps(VALID_STRATEGY, ensure_ascii=False), pydantic=wrong)

    # 类型不对 → 走 raw 解析这条路，而不是把 Copy 当成 MarketStrategy 返回
    assert isinstance(structured_output(output, MarketStrategy), MarketStrategy)


# ------------------------------------------------------------------ output_text


def test_output_text_prefers_raw() -> None:
    """TC-U-027 补充：事件流水写的是 `raw` 文本（REQ-005）。"""
    assert output_text(_output(raw="  报告正文  ")) == "报告正文"


def test_output_text_probes_alternative_attribute_names() -> None:
    """TC-U-027 补充：CrewAI 换字段名时按白名单探测，而不是退回 `repr()`（REQ-005）。

    `repr()` 会写出 `namespace(raw='…')` 这种在流水里毫无价值的字符串；
    排障时宁可看到真正的正文，或者干脆看到「空产出」。
    """
    assert output_text(SimpleNamespace(answer="别的形状")) == "别的形状"
    assert output_text(SimpleNamespace(content="正文")) == "正文"


def test_output_text_never_returns_repr_or_empty() -> None:
    """TC-U-027 补充：取不到文本时给「空产出」，而不是 `repr` 或不写（REQ-005）。"""
    assert output_text(_output(raw="   ")) == "(空产出)"
    assert output_text(SimpleNamespace()) == "(空产出)"
    assert output_text(None) == "(空产出)"
    assert output_text("") == "(空产出)"
