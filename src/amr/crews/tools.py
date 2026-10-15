"""检索工具的构造入口。

放在智能体层而不是适配层：「哪个 Agent 挂哪些工具」是角色定义的一部分，
和 role/goal/backstory 一样属于 `crews/`；适配层只负责「怎么把 Crew 跑起来」。

**必须延迟构造**：`SerperDevTool()` 在构造时就要读 `SERPER_API_KEY`，
放在模块顶层会让「没有检索密钥」直接把 import 搞崩——单元测试、离线系统测试、
甚至只想跑 `/healthz` 的进程都会受牵连。源项目的 import 期副作用就是这么来的。
"""

from __future__ import annotations

from typing import Any

__all__ = ["default_search_tools"]


def default_search_tools() -> list[Any]:
    """搜索 + 网页抓取两件套。只有真正要跑调研 Agent 时才会调到。"""
    from crewai_tools import ScrapeWebsiteTool, SerperDevTool

    return [SerperDevTool(), ScrapeWebsiteTool()]
