"""阶段二：内容创作 Crew（REQ-006 / REQ-007）。

三个结构化任务分别绑定 `output_json`；文案任务用 Task 的 `context` **显式声明**
依赖战略与创意两个任务，而不是把上游产出手工拼进 prompt ——
后者的脆弱之处在于：改了上游任务的描述，依赖它的 prompt 不会跟着变。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from crewai import Agent, Crew, Process, Task
from crewai.agents.agent_builder.base_agent import BaseAgent
from crewai.project import CrewBase, agent, crew, task

from amr.crews._output import output_text
from amr.crews.tools import default_search_tools
from amr.domain.inputs import CrewInputs
from amr.domain.models import CampaignIdea, Copy, MarketStrategy

__all__ = ["ContentCreatorCrew"]

TASK_UNDERSTANDING = "project_understanding_task"
TASK_STRATEGY = "marketing_strategy_task"
TASK_CAMPAIGN = "campaign_idea_task"
TASK_COPY = "copy_creation_task"


@CrewBase
class ContentCreatorCrew:
    """营销战略师 + 创意内容创作师：产出战略、创意与文案。"""

    agents: list[BaseAgent]
    tasks: list[Task]

    # `@CrewBase` 在运行时注入这两个属性（从同目录 config/*.yaml 读），
    # 静态检查看不到，这里显式声明——这样属性名写错仍然会被 mypy 抓到。
    if TYPE_CHECKING:
        agents_config: dict[str, Any]
        tasks_config: dict[str, Any]

    def __init__(
        self,
        model: Any,
        *,
        tools: Sequence[Any] | None = None,
        on_task_complete: Callable[[str, Any], None] | None = None,
    ) -> None:
        self._model = model
        self._tools: list[Any] | None = list(tools) if tools is not None else None
        self._on_task_complete = on_task_complete
        self.outputs: dict[str, Any] = {}

    # ------------------------------------------------------------ 装配

    @agent
    def chief_marketing_strategist(self) -> Agent:
        return Agent(
            config=self.agents_config["chief_marketing_strategist"],
            verbose=True,
            llm=self._model,
            tools=self._resolve_tools(),
        )

    @agent
    def creative_content_creator(self) -> Agent:
        # 文案创作不需要联网：给了工具反而会让它去检索、拉长链路
        return Agent(
            config=self.agents_config["creative_content_creator"],
            verbose=True,
            llm=self._model,
        )

    @task
    def project_understanding_task(self) -> Task:
        return Task(
            config=self.tasks_config[TASK_UNDERSTANDING],
            callback=self._capture(TASK_UNDERSTANDING),
        )

    @task
    def marketing_strategy_task(self) -> Task:
        return Task(
            config=self.tasks_config[TASK_STRATEGY],
            callback=self._capture(TASK_STRATEGY),
            output_json=MarketStrategy,
        )

    @task
    def campaign_idea_task(self) -> Task:
        return Task(
            config=self.tasks_config[TASK_CAMPAIGN],
            callback=self._capture(TASK_CAMPAIGN),
            output_json=CampaignIdea,
        )

    @task
    def copy_creation_task(self) -> Task:
        return Task(
            config=self.tasks_config[TASK_COPY],
            callback=self._capture(TASK_COPY),
            # 显式声明依赖：框架保证上游产出传到本任务，不靠手工拼 prompt
            context=[self.marketing_strategy_task(), self.campaign_idea_task()],
            output_json=Copy,
        )

    @crew
    def crew(self) -> Crew:
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            verbose=True,
        )

    # ------------------------------------------------------------ 执行

    def kickoff(self, inputs: CrewInputs) -> str:
        """跑一次内容创作，返回最后一个任务的产出文本。"""
        result = self.crew().kickoff(inputs=inputs.model_dump(mode="json"))
        return output_text(result)

    # ------------------------------------------------------------ 内部

    def _resolve_tools(self) -> list[Any]:
        if self._tools is None:
            self._tools = default_search_tools()
        return self._tools

    def _capture(self, key: str) -> Callable[[Any], None]:
        def callback(output: Any) -> None:
            self.outputs[key] = output
            if self._on_task_complete is not None:
                self._on_task_complete(key, output)

        return callback
