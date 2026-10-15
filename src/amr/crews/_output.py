"""从 CrewAI 的输出对象里安全地取文本与结构化结果。

CrewAI 的输出对象在不同版本间字段会变（`raw` / `pydantic` / `json_dict` …），
把取值逻辑收在这两个函数里，升级 CrewAI 时只需要改这一处。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from amr.domain.errors import SchemaValidationError

__all__ = ["output_text", "structured_output"]


def output_text(output: Any) -> str:
    """取任务产出的文本形式，用于写事件流水。"""
    raw = getattr(output, "raw", None)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    text = str(output).strip()
    return text or "(空产出)"


def structured_output[ModelT: BaseModel](output: Any, model: type[ModelT]) -> ModelT:
    """把任务产出解析成结构化模型，失败抛 `SchemaValidationError`（REQ-006）。

    两条路径，按可靠性排序：
    1. CrewAI 已用 `output_json` 解析好的 `pydantic` 字段——直接用，最可靠；
    2. 退回把 `raw` 文本按 JSON 解析——模型偶尔会把 JSON 包在说明文字里，
       这条路能救回来一部分。

    两条都失败就抛错，**绝不**把裸文本当成结构化结果传下去。
    """
    parsed = getattr(output, "pydantic", None)
    if isinstance(parsed, model):
        return parsed

    text = output_text(output)
    try:
        return model.model_validate_json(text)
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in err["loc"]) for err in exc.errors()})
        raise SchemaValidationError(
            f"{model.__name__} 解析失败，字段：{', '.join(fields) or '<未知>'}"
            f"（产出不是合法 JSON 或字段不符）",
            context={"model": model.__name__, "fields": fields},
        ) from exc
