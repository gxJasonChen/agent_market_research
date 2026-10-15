"""智能体层：CrewAI 的声明式角色与任务（spec/010 §4.3）。

Agent 的 role / goal / backstory 与 Task 的 description / expected_output 全部放在
各子包的 `config/{agents,tasks}.yaml` 里——新增或调整一个角色不需要改流程代码（REQ-007）。

`crewai` 只允许出现在本包与 `adapters/{crewai_runner,llm_factory}.py`，
由 `tests/unit/test_architecture.py` 守护。
"""

from amr.crews.content_creator.crew import ContentCreatorCrew
from amr.crews.market_analyst.crew import MarketAnalystCrew

__all__ = ["ContentCreatorCrew", "MarketAnalystCrew"]
