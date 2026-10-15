"""两个 Crew 的声明式配置与装配校验。

守护需求：REQ-006（结构化输出绑定）、REQ-007（双 Crew 编排与显式依赖）。
用例编号：TC-U-017 ~ TC-U-019。

全部离线：构造 Agent 用的模型是个指向 `127.0.0.1:1` 的 `crewai.LLM`，
**构造它不会发起任何请求**（只有 `call()` 才会），工具则显式传 `[]` 绕开检索密钥。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from amr.config import PROJECT_ROOT
from amr.crews import ContentCreatorCrew, MarketAnalystCrew
from amr.crews.content_creator.crew import (
    TASK_CAMPAIGN,
    TASK_COPY,
    TASK_STRATEGY,
    TASK_UNDERSTANDING,
)
from amr.crews.market_analyst.crew import TASK_RESEARCH
from amr.domain.inputs import CrewInputs
from amr.domain.models import CampaignIdea, Copy, MarketStrategy

CREWS_DIR = PROJECT_ROOT / "src" / "amr" / "crews"
CREW_PACKAGES = ("market_analyst", "content_creator")

# 匹配 YAML 里的 {变量} 占位符；CrewAI 用这对花括号做 kickoff(inputs=...) 的替换
PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


@pytest.fixture(scope="module")
def model() -> Any:
    """离线模型对象：能通过 CrewAI 的 pydantic 校验，但不会被真的调用。"""
    from crewai import LLM

    return LLM(model="gpt-4o-mini", base_url="http://127.0.0.1:1/v1", api_key="NA")


@pytest.fixture(scope="module")
def analyst(model: Any) -> MarketAnalystCrew:
    return MarketAnalystCrew(model, tools=[])


@pytest.fixture(scope="module")
def creator(model: Any) -> ContentCreatorCrew:
    return ContentCreatorCrew(model, tools=[])


def _yaml_files() -> list[Path]:
    files: list[Path] = []
    for package in CREW_PACKAGES:
        files.extend(sorted((CREWS_DIR / package / "config").glob("*.yaml")))
    return files


# ------------------------------------------------------------------ TC-U-017


def test_all_crew_yaml_files_exist_and_parse() -> None:
    """TC-U-017 四个 YAML 都存在且能解析（REQ-007）。"""
    files = _yaml_files()
    assert len(files) == 4, f"期望 4 个配置文件，实际 {[str(p) for p in files]}"

    for path in files:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict) and loaded, f"{path.name} 解析结果为空"


@pytest.mark.parametrize("path", _yaml_files(), ids=lambda p: f"{p.parent.parent.name}/{p.name}")
def test_placeholders_are_crew_input_fields(path: Path) -> None:
    """TC-U-017 所有 `{占位符}` 必须是 `CrewInputs` 的字段名（REQ-007）。

    占位符是运行期替换的：拼错一个字母不会报错，只会让模型拿到字面量 `{customer_domain}`
    然后开始编。这类错误在真实调用里很贵，所以用静态检查挡住。
    """
    text = path.read_text(encoding="utf-8")
    placeholders = set(PLACEHOLDER.findall(text))
    allowed = set(CrewInputs.model_fields)

    unknown = placeholders - allowed
    assert not unknown, f"{path.name} 出现未知占位符：{sorted(unknown)}，可用：{sorted(allowed)}"


def test_placeholders_actually_used() -> None:
    """TC-U-017 补充：任务描述里确实用到了业务变量，而不是写死的示例（REQ-007）。"""
    used: set[str] = set()
    for path in _yaml_files():
        used |= set(PLACEHOLDER.findall(path.read_text(encoding="utf-8")))

    assert {"customer_domain", "project_description"} <= used


def test_agents_config_declares_expected_roles() -> None:
    """TC-U-017 补充：角色齐全（1 + 2 个 Agent），且都声明了 role/goal/backstory（REQ-007）。"""
    analyst_agents = yaml.safe_load(
        (CREWS_DIR / "market_analyst" / "config" / "agents.yaml").read_text(encoding="utf-8")
    )
    creator_agents = yaml.safe_load(
        (CREWS_DIR / "content_creator" / "config" / "agents.yaml").read_text(encoding="utf-8")
    )

    assert set(analyst_agents) == {"lead_market_analyst"}
    assert set(creator_agents) == {"chief_marketing_strategist", "creative_content_creator"}

    for config in (analyst_agents, creator_agents):
        for name, spec in config.items():
            assert spec.get("role"), f"{name} 缺少 role"
            assert spec.get("goal"), f"{name} 缺少 goal"
            assert spec.get("backstory"), f"{name} 缺少 backstory"


def test_analyst_agent_uses_real_current_date() -> None:
    """TC-U-017 补充：调研类 Agent 开启日期注入，避免模型臆测年份（REQ-007）。"""
    config = yaml.safe_load(
        (CREWS_DIR / "market_analyst" / "config" / "agents.yaml").read_text(encoding="utf-8")
    )

    assert config["lead_market_analyst"]["inject_date"] is True


# ------------------------------------------------------------------ TC-U-018


def test_strategy_task_binds_market_strategy_schema(creator: ContentCreatorCrew) -> None:
    """TC-U-018 战略任务绑定 MarketStrategy（REQ-006）。"""
    assert creator.marketing_strategy_task().output_json is MarketStrategy


def test_campaign_task_binds_campaign_idea_schema(creator: ContentCreatorCrew) -> None:
    """TC-U-018 创意任务绑定 CampaignIdea（REQ-006）。"""
    assert creator.campaign_idea_task().output_json is CampaignIdea


def test_copy_task_binds_copy_schema(creator: ContentCreatorCrew) -> None:
    """TC-U-018 文案任务绑定 Copy（REQ-006）。"""
    assert creator.copy_creation_task().output_json is Copy


def test_understanding_task_has_no_structured_output(creator: ContentCreatorCrew) -> None:
    """TC-U-018 补充：理解类任务保持自由文本——强行结构化只会给模型加无谓的约束（REQ-006）。"""
    assert creator.project_understanding_task().output_json is None


def test_research_task_has_no_structured_output(analyst: MarketAnalystCrew) -> None:
    """TC-U-018 补充：调研报告是长文，不做结构化（REQ-006）。"""
    assert analyst.research_task().output_json is None


# ------------------------------------------------------------------ TC-U-019


def test_copy_task_declares_context_dependency(creator: ContentCreatorCrew) -> None:
    """TC-U-019 文案任务**显式声明**依赖战略与创意两个任务（REQ-007）。

    这条断言的是「依赖由框架管理」，而不是「把上游输出拼进 prompt」——
    后者在上游任务改描述时会静默失效。
    """
    copy_task = creator.copy_creation_task()

    assert copy_task.context is not None
    assert len(copy_task.context) == 2

    upstream = {task.description for task in copy_task.context}
    assert creator.marketing_strategy_task().description in upstream
    assert creator.campaign_idea_task().description in upstream


def test_task_names_are_stable() -> None:
    """TC-U-019 补充：任务键名是事件流水与结果提取的约定，改名要有意识地改（REQ-005）。"""
    assert TASK_RESEARCH == "research_task"
    assert {TASK_UNDERSTANDING, TASK_STRATEGY, TASK_CAMPAIGN, TASK_COPY} == {
        "project_understanding_task",
        "marketing_strategy_task",
        "campaign_idea_task",
        "copy_creation_task",
    }


def test_crews_build_sequential_with_declared_agents(
    analyst: MarketAnalystCrew, creator: ContentCreatorCrew
) -> None:
    """TC-U-019 补充：两个 Crew 都是顺序执行，且 Agent/Task 数量与声明一致（REQ-007）。"""
    from crewai import Process

    analyst_crew = analyst.crew()
    assert analyst_crew.process is Process.sequential
    assert len(analyst_crew.agents) == 1
    assert len(analyst_crew.tasks) == 1

    creator_crew = creator.crew()
    assert creator_crew.process is Process.sequential
    assert len(creator_crew.agents) == 2
    assert len(creator_crew.tasks) == 4
