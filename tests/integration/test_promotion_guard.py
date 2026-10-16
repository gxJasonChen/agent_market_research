"""提升守卫跑在真实 git 仓库上（spec/000 §2.7）。

放在集成层的原因：这是「脚本 + 真实 git」的跨组件验证，不需要 MySQL / Redis /
模型网关，因此和 `make test` 一起进 CI 门禁（NFR-001）。纯决策逻辑在
`tests/unit/test_promotion_guard.py`；这里只证明管道是通的——ref 解析、
不变量查询、退出码、证据输出。

用例编号：TC-I-013（集成边通过 / 分叉被拦）、TC-I-014（发布边在真实 `--no-ff`
发布之后仍然通过）、TC-I-015（守卫是只读的）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from amr.config import PROJECT_ROOT

SCRIPT = PROJECT_ROOT / "scripts" / "check_promotion.py"


def _git_env() -> dict[str, str]:
    """与运行者本机的 git 配置隔离：用例不该依赖 global config（spec/040 §5）。"""
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "AMR 测试",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "AMR 测试",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env=_git_env(),
        check=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "commit", "--allow-empty", "--no-verify", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """最小仓库：`main` / `development` / `bugfix` 都停在第一个提交上。"""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _commit(root, "first commit")
    for branch in ("development", "bugfix"):
        _git(root, "branch", branch)
    return root


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), *args],
        capture_output=True,
        text=True,
        env=_git_env(),
    )


def _advance_bugfix(repo: Path, *messages: str) -> None:
    """在 `bugfix` 上追加提交——模拟开发 Agent 的一轮工作。"""
    _git(repo, "checkout", "bugfix")
    for message in messages:
        _commit(repo, message)


def _accept_into_development(repo: Path) -> None:
    """模拟 QA 的集成边：`--ff-only`，见 spec/000 §2.4。"""
    _git(repo, "checkout", "development")
    _git(repo, "merge", "--ff-only", "bugfix")


def _release_to_main(repo: Path, tag: str) -> None:
    """模拟人工发布：`--no-ff` + tag，见 spec/000 §2.5。"""
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "development", "-m", f"release: {tag}")
    _git(repo, "tag", "-a", tag, "-m", tag)


def test_integration_edge_passes_when_target_is_ancestor(repo: Path) -> None:
    """TC-I-013 正常集成：`bugfix` 领先且没有分叉 → 允许提升，并给出该执行的命令。"""
    _advance_bugfix(repo, "feat: 第一个改动", "feat: 第二个改动")

    result = _run(repo, "bugfix", "development")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "--ff-only" in result.stdout
    assert "允许提升" in result.stdout


def test_diverged_target_is_blocked_and_named(repo: Path) -> None:
    """TC-I-013 分叉：`development` 上有 `bugfix` 没有的提交 → 拦下并点名。"""
    _git(repo, "checkout", "development")
    _commit(repo, "chore: 直接提交到 development")
    _advance_bugfix(repo, "feat: 我的改动")

    result = _run(repo, "bugfix", "development")

    assert result.returncode == 1
    assert "直接提交到 development" in result.stdout


def test_release_edge_passes_after_a_real_no_ff_release(repo: Path) -> None:
    """TC-I-014 第二次发布：`main` 上有前一次 `--no-ff` 的合并提交，守卫不能报假警。

    这是设计里最容易写错的一条：合并提交只存在于 `main` 上，
    因此「`main` 是 `development` 的祖先」永远不成立（spec/000 §2.3）。
    """
    _advance_bugfix(repo, "feat: 第一批")
    _accept_into_development(repo)
    _release_to_main(repo, "v0.1.0")

    _advance_bugfix(repo, "feat: 第二批")
    _accept_into_development(repo)

    result = _run(repo, "development", "main", "--tag", "v0.2.0")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "允许提升" in result.stdout


def test_release_edge_blocks_private_commit_on_main(repo: Path) -> None:
    """TC-I-014 不变量 2：`main` 上直接提交的代码必须被拦下（而不是被合并悄悄带过）。"""
    _advance_bugfix(repo, "feat: 正常改动")
    _accept_into_development(repo)
    _git(repo, "checkout", "main")
    _commit(repo, "hotfix: 直接提交到 main")

    result = _run(repo, "development", "main")

    assert result.returncode == 1
    assert "直接提交到 main" in result.stdout


def test_wrong_direction_is_rejected(repo: Path) -> None:
    """TC-I-013 方向白名单：反向提升连评估都不做（spec/000 §2.2）。"""
    result = _run(repo, "development", "bugfix")

    assert result.returncode == 1
    assert "不允许的提升方向" in result.stdout + result.stderr


def test_guard_is_read_only(repo: Path) -> None:
    """TC-I-015 守卫只诊断不动手：跑完之后 ref 与工作区必须原样。

    合并权在 spec/000 §2.2 里是分角色的（A 边属 QA、B 边属人），
    工具一旦顺手把合并做了，这个分工就失效了。
    """
    _advance_bugfix(repo, "feat: 待提升")
    branches = ("main", "development", "bugfix")
    before = {name: _git(repo, "rev-parse", name) for name in branches}

    assert _run(repo, "bugfix", "development").returncode == 0

    after = {name: _git(repo, "rev-parse", name) for name in branches}
    assert after == before
    assert _git(repo, "status", "--porcelain") == ""
