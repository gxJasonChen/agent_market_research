"""前端构建产物的系统级验证（spec/040 §2.4 TC-S-005 / REQ-012）。

前端没有 Python 侧的单元测试入口，它的「能不能交付」用真实的 type-check 与
build 来判定：类型不过等于契约对不上，构建不过等于交付物不可用。
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from amr.config import PROJECT_ROOT

FRONTEND = PROJECT_ROOT / "src" / "web"

pytestmark = pytest.mark.system


def _require_node() -> None:
    if shutil.which("npm") is None:
        pytest.skip("npm 不可用，跳过前端构建验证")
    if not (FRONTEND / "package.json").exists():
        pytest.skip("src/web 尚未交付（并行开发中）")


def _run(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_frontend_type_check_passes() -> None:
    """TC-S-005 前端类型检查通过（REQ-012）：类型对不上就是契约对不上。"""
    _require_node()
    if not (FRONTEND / "node_modules").exists():
        install = _run(["npm", "install"], timeout=900)
        assert install.returncode == 0, f"npm install 失败：\n{install.stdout}\n{install.stderr}"

    result = _run(["npm", "run", "type-check"])
    assert result.returncode == 0, f"npm run type-check 失败：\n{result.stdout}\n{result.stderr}"


def test_frontend_production_build_succeeds() -> None:
    """TC-S-005 生产构建成功且产出 index.html（REQ-012）。"""
    _require_node()
    if not (FRONTEND / "node_modules").exists():
        install = _run(["npm", "install"], timeout=900)
        assert install.returncode == 0, f"npm install 失败：\n{install.stdout}\n{install.stderr}"

    result = _run(["npm", "run", "build"])
    assert result.returncode == 0, f"npm run build 失败：\n{result.stdout}\n{result.stderr}"
    assert (FRONTEND / "dist" / "index.html").exists(), "构建产物缺少 dist/index.html"


def test_frontend_api_contract_matches_spec() -> None:
    """TC-S-005 补充：前端调用的路径与 spec/022 一致（REQ-012）。

    这条用文本断言而不是渲染断言，是刻意的：它防止前端悄悄改接口路径
    （例如写成 `/crew` 或 `/api/jobs`），而这类改动在构建里是静默通过的。
    """
    _require_node()
    sources = list((FRONTEND / "src").rglob("*.ts")) + list((FRONTEND / "src").rglob("*.vue"))
    assert sources, "前端源码目录为空"

    blob = "\n".join(path.read_text(encoding="utf-8") for path in sources)
    assert "/api/crew" in blob, "前端没有调用 POST /api/crew"
    assert "/api/crew/" in blob, "前端没有调用 GET /api/crew/{job_id}"
