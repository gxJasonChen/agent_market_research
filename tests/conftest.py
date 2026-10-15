"""全局 pytest 配置与钩子。

这里只放**跨层共用**的东西；各层自己的 fixture 放在各自的 `conftest.py` 里，
避免这个文件变成并行开发时的冲突热点（spec/030 §3）。
"""

from __future__ import annotations

from typing import Any

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-real-llm",
        action="store_true",
        default=False,
        help="额外运行真实模型网关的系统测试（会产生真实调用与费用，默认关闭）",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """按路径自动打 `system` 标记，并处理 `--run-real-llm` 开关。

    标记必须自动打：写在各用例上的话，漏一个就会被 `make test-system -m system`
    静默筛掉——那种「没跑」看起来和「跑过且通过」一模一样。放在 conftest 的
    `pytestmark` 里也不行（conftest 的 pytestmark 不作用于测试模块），
    所以在这里按文件路径统一判定。
    """
    run_real_llm = config.getoption("--run-real-llm")
    skip_real_llm = pytest.mark.skip(reason="需要 --run-real-llm 才运行（会真实调用模型网关）")

    for item in items:
        path = str(getattr(item, "path", item.fspath)).replace("\\", "/")
        if "/tests/system/" in path:
            item.add_marker(pytest.mark.system)
        # 默认跳过真实模型调用：花钱、慢、结果不确定，不能进本地门禁与 CI（NFR-001）
        if not run_real_llm and "real_llm" in item.keywords:
            item.add_marker(skip_real_llm)


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - 目前无异步用例，留作扩展
    return "asyncio"


def _noop(*_: Any, **__: Any) -> None:  # pragma: no cover
    return None
