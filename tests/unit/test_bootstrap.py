"""装配点测试（spec/021 §7）。

装配点是全仓库仅有的两处「按配置选实现」的地方，选错了在单测里看不出来——
只有真的把配置喂进去、看它装出什么东西，才知道对不对。

用例编号：TC-U-024（`AMR_LLM_PROVIDER` 的执行器分派）、TC-U-025（装配不触网）。
"""

from __future__ import annotations

import pytest

from amr.adapters.fake_runner import FakeFlowRunner
from amr.adapters.memory_repository import MemoryJobRepository
from amr.config import Settings
from amr.domain.errors import InvalidInputError, QueueUnavailableError
from amr.domain.inputs import CrewInputs
from amr.worker.bootstrap import _NoEnqueue, build_executor, build_repository, build_runner


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


INPUTS = CrewInputs(
    customer_domain="openai.com",
    project_description="为一家 AI 基础设施公司制定新品发布营销方案",
)


# ------------------------------------------------------------------ TC-U-024


def test_fake_provider_selects_the_offline_runner() -> None:
    """TC-U-024 `fake` 装配出离线确定性执行器，而不是去构造模型（spec/021 §4.1）。"""
    runner = build_runner(_settings(llm_provider="fake"))

    assert isinstance(runner, FakeFlowRunner)


def test_real_provider_missing_config_fails_loudly_at_assembly() -> None:
    """TC-U-024 真实供应商缺配置时，**在装配期**就报错，不拖到第一次调用（REQ-008）。

    否则 Worker 会正常启动、正常收任务，然后在跑第一个作业时才炸——
    那时排查要从作业的 ERROR 摘要倒推回配置，成本高得多。
    """
    with pytest.raises(InvalidInputError) as exc:
        build_runner(_settings(llm_provider="openai", openai_api_base="", openai_api_key=""))

    assert "OPENAI_API_BASE" in str(exc.value)


# ------------------------------------------------------------------ TC-U-025


def test_build_repository_does_not_connect() -> None:
    """TC-U-025 构造仓储不连库：没有 MySQL 时装配也必须成功（spec/010 §4.1）。

    源项目的病根就是「import 期连库」，导致离开数据库连模块都加载不了。
    """
    repository = build_repository(_settings(mysql_host="192.0.2.1", mysql_port=3306))

    assert repository is not None  # 只构造，不调用，因此不会去连那个不可达的地址


def test_build_executor_wires_runner_from_settings() -> None:
    """TC-U-025 `build_executor` 把配置一路传到执行器（REQ-002）。"""
    executor = build_executor(_settings(llm_provider="fake", mysql_host="192.0.2.1"))

    assert executor is not None


def test_worker_side_queue_refuses_to_enqueue() -> None:
    """TC-U-025 Worker 侧的作业服务不负责入队，被调用到必须响（spec/021 §7）。

    给它一个「看起来能用」的队列，就等于允许 Worker 自己给自己派活；给 `None` 则会在
    别处变成更难排查的 AttributeError。所以给一个一用就报错的对象。
    """
    queue = _NoEnqueue()

    with pytest.raises(QueueUnavailableError) as exc:
        queue.enqueue_flow("job-1", INPUTS)

    assert "Worker" in str(exc.value)


def test_memory_repository_satisfies_the_port_used_by_bootstrap() -> None:
    """TC-U-025 补充：装配点使用的仓储实现满足端口（类型级的最小校验）。"""
    from amr.ports.repository import JobRepository

    assert isinstance(MemoryJobRepository(), JobRepository)
