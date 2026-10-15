"""阶段一：市场调研 Crew（REQ-007）。

角色与任务全在 `config/*.yaml` 里声明；本文件只负责把它们组装起来，
并把「任务完成」这件事告诉外部（由 `CrewAIFlowRunner` 转成事件流水）。
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

__all__ = ["MarketAnalystCrew"]

TASK_RESEARCH = "research_task"


@CrewBase
class MarketAnalystCrew:
    """首席市场分析师：产出一份全面调研报告（自由文本）。"""

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
        """`tools` 默认懒构造：测试可以传 `[]` 在完全离线、无检索密钥的情况下组装 Agent。"""
        self._model = model
        self._tools: list[Any] | None = list(tools) if tools is not None else None
        self._on_task_complete = on_task_complete
        self.outputs: dict[str, Any] = {}

    # ------------------------------------------------------------ 装配

    @agent
    def lead_market_analyst(self) -> Agent:
        return Agent(
            config=self.agents_config["lead_market_analyst"],
            verbose=True,
            llm=self._model,
            tools=self._resolve_tools(),
        )

    @task
    def research_task(self) -> Task:
        return Task(
            config=self.tasks_config[TASK_RESEARCH],
            callback=self._capture(TASK_RESEARCH),
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
        """跑一次调研，返回报告全文。"""
        result = self.crew().kickoff(inputs=inputs.model_dump(mode="json"))
        return output_text(result)

    # ------------------------------------------------------------ 内部

    def _resolve_tools(self) -> list[Any]:
        if self._tools is None:
            self._tools = default_search_tools()
        return self._tools

    def _capture(self, key: str) -> Callable[[Any], None]:
        """生成该任务专用的回调：先留档，再通知外部。"""

        def callback(output: Any) -> None:
            self.outputs[key] = output
            if self._on_task_complete is not None:
                self._on_task_complete(key, output)

        return callback
