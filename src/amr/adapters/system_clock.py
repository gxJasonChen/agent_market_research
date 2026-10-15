"""`Clock` 与 `IdGenerator` 的生产实现（spec/021 §5）。

生产代码取时间/生成 ID 只能走这两个类：直接 `datetime.now()` / `uuid4()`
会让测试无法复现，断言只能写成「大概是现在」这种没有价值的形式。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

__all__ = ["SystemClock", "Uuid4Generator"]


class SystemClock:
    """系统时钟，一律返回带 UTC 时区的 `datetime`。

    带时区是硬要求：源项目存的是 naive datetime，前端拿到后按本地时区渲染，
    跨时区会整体偏移。
    """

    def now(self) -> datetime:
        return datetime.now(UTC)


class Uuid4Generator:
    """UUID4 字符串生成器。"""

    def new_id(self) -> str:
        return str(uuid.uuid4())
