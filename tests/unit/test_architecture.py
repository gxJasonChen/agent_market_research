"""架构守护测试：用 AST 扫描代替「大家记得遵守」。

写在文档里的分层约束，如果没有测试守着，几周之后就不成立了。
这里把 spec/000 §5 与 spec/010 §1 的铁律变成会失败的用例。

用例编号：TC-U-022（services 不得依赖适配器/框架）、TC-U-023（domain 零外部依赖）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from amr.config import PROJECT_ROOT

SRC = PROJECT_ROOT / "src" / "amr"

# 第三方框架只能出现在适配层与智能体层；业务层认的是 Protocol，不是它们
FRAMEWORKS = frozenset({"crewai", "crewai_tools", "celery", "redis", "mysql", "fastapi", "uvicorn"})

# 各层允许 import 的本项目包（依赖只能向内）
LAYER_RULES: dict[str, frozenset[str]] = {
    "domain": frozenset(),  # 谁也不许依赖
    "ports": frozenset({"amr.domain"}),
    "services": frozenset({"amr.domain", "amr.ports"}),
    "adapters": frozenset({"amr.domain", "amr.ports", "amr.crews", "amr.config"}),
    "crews": frozenset({"amr.domain", "amr.ports", "amr.config"}),
    # api 与 worker 同属接入层（spec/010 §3 把它们画在同一个框里），互相可见是设计的一部分：
    # Celery app 只在 worker/tasks.py 声明一次，API 进程作为生产者复用它——
    # 各声明一份才是真问题（生产者与消费者的任务名/队列配置会漂移）。
    # 真正要拦的是接入层之下的任何一层去 import 它们，那由各自的规则负责。
    "api": frozenset(
        {"amr.domain", "amr.ports", "amr.services", "amr.adapters", "amr.config", "amr.worker"}
    ),
    "worker": frozenset(
        {"amr.domain", "amr.ports", "amr.services", "amr.adapters", "amr.config", "amr.api"}
    ),
}


def _python_files(layer: str) -> list[Path]:
    directory = SRC / layer
    if not directory.exists():
        return []
    return sorted(path for path in directory.rglob("*.py") if "__pycache__" not in path.parts)


def _imported_modules(path: Path) -> set[str]:
    """返回该文件 import 的所有模块名（`from a.b import c` → `a.b`）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        # 相对 import（level > 0）在本项目内，按包内引用处理，不产生外部依赖，因此只收 level == 0
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def _top_level(module: str) -> str:
    return module.split(".", 1)[0]


def _owned_by(module: str, allowed: frozenset[str]) -> bool:
    """`amr.xxx` 是否落在允许集合内（`amr.config` 之类按精确前缀判断）。"""
    if not module.startswith("amr"):
        return True
    if module == "amr":
        # 只有包自身的 `__init__`（版本号），不构成对任何一层的依赖
        return True
    return any(module == item or module.startswith(f"{item}.") for item in allowed)


ALL_LAYERS = sorted(LAYER_RULES)


@pytest.mark.parametrize("layer", ALL_LAYERS)
def test_no_layer_depends_on_outer_layer(layer: str) -> None:
    """TC-U-022 依赖只能向内：每层只允许 import 规则里列出的本项目包（spec/010 §1）。

    同层内部的互相 import（如 `domain.models` 引 `domain.status`）是正常的，一并放行。
    """
    allowed = LAYER_RULES[layer] | {f"amr.{layer}"}
    violations: list[str] = []

    for path in _python_files(layer):
        for module in _imported_modules(path):
            if module.startswith("amr") and not _owned_by(module, allowed):
                violations.append(f"{path.relative_to(PROJECT_ROOT)} → {module}")

    assert not violations, f"{layer} 层出现了向外的依赖：\n" + "\n".join(violations)


@pytest.mark.parametrize("layer", ["domain", "ports", "services"])
def test_business_layers_do_not_import_frameworks(layer: str) -> None:
    """TC-U-022 业务层不得 import 任何框架——它们只认端口（NFR-004）。

    `crewai` / `celery` / `mysql` / `redis` 一旦渗进 services，
    离线单测立刻做不下去，换实现也要动业务代码。
    """
    violations: list[str] = []

    for path in _python_files(layer):
        for module in _imported_modules(path):
            if _top_level(module) in FRAMEWORKS:
                violations.append(f"{path.relative_to(PROJECT_ROOT)} → {module}")

    assert not violations, f"{layer} 层出现了框架依赖：\n" + "\n".join(violations)


def test_domain_has_zero_third_party_dependencies() -> None:
    """TC-U-023 `domain/` 只允许标准库 + pydantic（spec/000 §5）。"""
    allowed_third_party = {"pydantic"}
    violations: list[str] = []

    for path in _python_files("domain"):
        for module in _imported_modules(path):
            root = _top_level(module)
            if module.startswith("amr") or root in allowed_third_party:
                continue
            if root in sys.stdlib_module_names:
                continue
            violations.append(f"{path.relative_to(PROJECT_ROOT)} → {module}")

    assert not violations, "domain 层出现了标准库/pydantic 之外的依赖：\n" + "\n".join(violations)


def test_services_do_not_import_adapters_explicitly() -> None:
    """TC-U-022 点名那条最容易被破坏的依赖：services → adapters（NFR-004）。"""
    violations: list[str] = []

    for path in _python_files("services"):
        for module in _imported_modules(path):
            if module.startswith("amr.adapters"):
                violations.append(f"{path.relative_to(PROJECT_ROOT)} → {module}")

    assert not violations, "services 层直接依赖了适配器：\n" + "\n".join(violations)


def test_layers_exist() -> None:
    """TC-U-023 补充：分层目录必须存在，否则上面的扫描会「因为没有文件而通过」。"""
    missing = [layer for layer in ALL_LAYERS if not (SRC / layer).exists()]
    assert not missing, f"缺少分层目录：{missing}"


def test_scanner_actually_finds_imports() -> None:
    """TC-U-022 补充：扫描器自身的有效性——防止它因为解析问题变成永远通过。

    拿一个已知含 import 的文件做正样本；扫不出东西说明扫描器坏了，
    上面那些「没发现违规」的结论也就不算数。
    """
    sample = SRC / "services" / "job_service.py"
    assert sample.exists()
    assert _imported_modules(sample), "扫描器没有解析出任何 import，架构守护形同虚设"
