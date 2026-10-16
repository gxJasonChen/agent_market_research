"""提升守卫跑在真实 git 仓库上（spec/000 §2.7）。

放在集成层的原因：这是「脚本 + 真实 git」的跨组件验证，不需要 MySQL / Redis /
模型网关，因此和 `make test` 一起进 CI 门禁（NFR-001）。纯决策逻辑在
`tests/unit/test_promotion_guard.py`；这里只证明管道是通的——ref 解析、
不变量查询、退出码、证据输出。

用例编号：TC-I-015（集成边通过 / 分叉被拦）、TC-I-016（发布边在真实 `--no-ff`
发布之后仍然通过）、TC-I-017（守卫是只读的）、TC-I-018（绕过发布边的合并被拦下）。

需求追溯：REQ-015（交付流程守卫）。
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
    """TC-I-015（REQ-015） 正常集成：`bugfix` 领先且没有分叉 → 允许提升，并给出该执行的命令。"""
    _advance_bugfix(repo, "feat: 第一个改动", "feat: 第二个改动")

    result = _run(repo, "bugfix", "development")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "--ff-only" in result.stdout
    assert "允许提升" in result.stdout


def test_diverged_target_is_blocked_and_named(repo: Path) -> None:
    """TC-I-015（REQ-015） 分叉：`development` 上有 `bugfix` 没有的提交 → 拦下并点名。"""
    _git(repo, "checkout", "development")
    _commit(repo, "chore: 直接提交到 development")
    _advance_bugfix(repo, "feat: 我的改动")

    result = _run(repo, "bugfix", "development")

    assert result.returncode == 1
    assert "直接提交到 development" in result.stdout


def test_release_edge_passes_after_a_real_no_ff_release(repo: Path) -> None:
    """TC-I-016（REQ-015） 第二次发布：`main` 上有前一次 `--no-ff` 的合并提交，守卫不能报假警。

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
    """TC-I-016（REQ-015） 不变量 2：`main` 上直接提交的代码必须被拦下（而不是被合并悄悄带过）。"""
    _advance_bugfix(repo, "feat: 正常改动")
    _accept_into_development(repo)
    _git(repo, "checkout", "main")
    _commit(repo, "hotfix: 直接提交到 main")

    result = _run(repo, "development", "main", "--tag", "v0.2.0")

    assert result.returncode == 1
    assert "直接提交到 main" in result.stdout


def test_merge_that_bypasses_the_release_edge_is_blocked(repo: Path) -> None:
    """TC-I-018（REQ-015） 绕过发布边、把 `bugfix` 直接 `--no-ff` 合进 `main`，必须被拦下。

    这次合并在 `main` 上留下的提交**全部来自 `bugfix`**，内容早就在 `development`
    上了——于是没有任何非合并提交会落进「目标分支私有提交」这个集合。守卫若用
    `--no-merges` 一刀切，它就彻底隐形，对着一次分叉报「允许提升」，
    而发布边没有 `--ff-only` 那一侧兜底，这是 `main` 侧唯一的机械校验。

    判别依据只能是与版本 tag 绑定的发布合并形态：正经发布按 §2.2 一定带 tag，
    绕过的那次没有。GitHub 上把 `bugfix` 的 PR 直接 merge 进 `main` 产生的是同一形状。
    """
    _advance_bugfix(repo, "feat: 第一批")
    _accept_into_development(repo)
    _release_to_main(repo, "v0.1.0")
    _advance_bugfix(repo, "feat: 第二批")
    _accept_into_development(repo)

    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "bugfix", "-m", "chore: 绕过发布边合进 main")

    result = _run(repo, "development", "main", "--tag", "v0.2.0")

    assert result.returncode == 1, result.stdout
    assert "绕过发布边合进 main" in result.stdout
    # 断言确切计数：正当的发布合并提交（v0.1.0）仍要放行，不能因为这次拦截被算成私货。
    # 原本这里写 `assert "0 个" in stdout`，匹配到的其实是「待提升提交」那一行——
    # 本场景里它恰好也是 0，所以断言恒真、测不到注释声称的东西。
    assert "目标分支私有提交：1 个（另有 1 个带 tag 的发布合并提交）" in result.stdout


def test_arbitrary_tag_does_not_launder_a_bypass_merge(repo: Path) -> None:
    """TC-I-018（REQ-015） 绕过发布边之后补一个**任意** tag，不能换来放行。

    判别发布合并提交的条件里，「带 tag」必须是「带 **SemVer 版本** tag」。只要
    「挂了 tag」就算数，补一句 `git tag wip` 就能让守卫主动背书那次绕过——它会渲染进
    「放行的发布合并提交」并打印「按 §2.2 放行」。白名单的判据必须与不变量 3 的
    管辖范围一致，否则 H1 堵掉的假阴性只差一条 `git tag`。
    """
    _advance_bugfix(repo, "feat: 第一批")
    _accept_into_development(repo)
    _release_to_main(repo, "v0.1.0")
    _advance_bugfix(repo, "feat: 第二批")
    _accept_into_development(repo)

    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "bugfix", "-m", "chore: 绕过发布边")
    _git(repo, "tag", "wip")

    result = _run(repo, "development", "main", "--tag", "v0.2.0")

    assert result.returncode == 1, result.stdout
    assert "绕过发布边" in result.stdout


def test_tag_pointing_at_a_tree_does_not_break_the_guard(repo: Path) -> None:
    """TC-I-018（REQ-015） tag 可以合法地指向 tree/blob，那时守卫不能整个崩掉。

    `git tag v9.9.9 <tree-sha>` 是合法的。这种 tag 送到 `merge-base --is-ancestor`
    会以 128 报错（「is a tree, not a commit」）而不是回 1，于是 `_is_ancestor` 抛异常，
    守卫**对所有边**一起失效——连与 tag 毫无关系的集成边自检都跑不了，报文还指不到成因。
    """
    _advance_bugfix(repo, "feat: 待提升")
    _git(repo, "tag", "v9.9.9", _git(repo, "rev-parse", "HEAD^{tree}"))

    result = _run(repo, "bugfix", "development")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "无法采集事实" not in result.stdout + result.stderr


def test_missing_target_branch_is_not_a_pass(repo: Path) -> None:
    """TC-I-015（REQ-015） 目标分支真的不存在时，退出码必须与「通过」可区分。

    bootstrap 是正常状态（新仓库第一次 push 到 `bugfix`，ci.yml 里专门有一步创建
    `development`），所以要放过而不是退 1——否则依赖 `branch-guard` 的 `open-pr`
    会被连带 skip，那一步永远到不了，交棒停摆。

    但放过**不等于通过**：退 0 会让一个取不到目标分支的环境把发布门禁静默变成绿灯，
    而这正是守卫最不该有的失效方式。跳过用 2，通过才是 0。
    """
    _git(repo, "branch", "-D", "development")
    _advance_bugfix(repo, "feat: 第一次推送")

    result = _run(repo, "bugfix", "development")

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.returncode != 0
    assert "还不存在" in result.stdout + result.stderr


def test_target_falls_back_to_origin_when_local_branch_absent(repo: Path) -> None:
    """TC-I-015（REQ-015） 本地没有目标分支时退回 `origin/<目标>`，不能当成 bootstrap。

    规格与手册给的调用形式**全是裸分支名**（`check_promotion.py development main --tag vX`），
    而它们出现的场合——CI 的 checkout、`git clone -b <分支>`、worktree——本地往往只有源分支。
    `RUNBOOK` §7.3 教的顺序更是「先跑守卫」再 `git checkout main`，那时本地根本没有 `main`。
    不退回的话，发布清单上的「守卫放行」会读到一个**假的通过**，连 `--tag` 都不被校验。
    """
    _advance_bugfix(repo, "feat: 批次一")
    _accept_into_development(repo)
    _release_to_main(repo, "v0.1.0")
    _advance_bugfix(repo, "feat: 批次二")
    _accept_into_development(repo)

    # 造出「只有 origin/main、没有本地 main」的检出
    _git(repo, "update-ref", "refs/remotes/origin/main", _git(repo, "rev-parse", "main"))
    _git(repo, "checkout", "-q", "bugfix")
    _git(repo, "branch", "-D", "main")

    result = _run(repo, "development", "main", "--tag", "v0.2.0")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "允许提升" in result.stdout


def test_tag_on_origin_main_is_not_misplaced_when_local_main_is_stale(repo: Path) -> None:
    """TC-I-018（REQ-015） 本地 `main` 陈旧时，不能把落在 `origin/main` 上的版本 tag 报成错位。

    多机协作里很常见：别人发完版，这台机器只 `git fetch --tags`，本地 `main` 没动。
    判据是全局不变量，就不该让一个可能过时的引用压过权威引用，更不该在两者不一致时
    静默取本地。
    """
    _advance_bugfix(repo, "feat: 批次一")
    _accept_into_development(repo)
    head = _git(repo, "rev-parse", "development")
    _git(repo, "update-ref", "refs/remotes/origin/main", head)
    _git(repo, "tag", "-a", "v1.0.0", "-m", "v1.0.0", head)

    result = _run(repo, "bugfix", "development")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "不在 main 上" not in result.stdout


def test_missing_main_is_fail_closed(repo: Path) -> None:
    """TC-I-018（REQ-015） 定位不到 `main` 时，不变量 3 必须显式报「未校验」而不是静默放过。

    静默返回空等于「不变量 3 不检查」，而这次改动刚把「缺东西就降级成不检查」
    定性为比直接报错危险——同一份实现不该给自己留一个这样的分支。
    """
    _advance_bugfix(repo, "feat: 待发布")
    _git(repo, "branch", "-m", "main", "trunk")  # 让 main 从此不存在

    result = _run(repo, "bugfix", "development")

    assert result.returncode == 1, result.stdout
    assert "定位不到 main" in result.stdout


def test_version_tag_outside_main_is_blocked(repo: Path) -> None:
    """TC-I-018（REQ-015） 不变量 3：版本 tag 打在非 `main` 分支上要被拦下。

    原本 tag 在实现里只以名字出现，只查格式与重名，从不看它指向哪个提交，
    所以「tag 只打在 main 上」这一句是零覆盖的。
    """
    _advance_bugfix(repo, "feat: 待发布")
    _git(repo, "tag", "-a", "v0.9.9", "-m", "v0.9.9")  # 打在 bugfix 上，不在 main

    result = _run(repo, "bugfix", "development")

    assert result.returncode == 1
    assert "v0.9.9" in result.stdout
    assert "不在 main 上" in result.stdout


def test_wrong_direction_is_rejected(repo: Path) -> None:
    """TC-I-015（REQ-015） 方向白名单：反向提升连评估都不做（spec/000 §2.2）。"""
    result = _run(repo, "development", "bugfix")

    assert result.returncode == 1
    assert "不允许的提升方向" in result.stdout + result.stderr


def test_unknown_branch_is_named_as_unknown(repo: Path) -> None:
    """TC-I-015（REQ-015） 分支名打错时说「不存在」，不要说「方向不允许」。

    后者会把人引去读 §2.2 的方向白名单，而真正的问题只是一个拼写错误。
    """
    result = _run(repo, "bugfix", "nosuchbranch")

    assert result.returncode == 1
    assert "分支不存在" in result.stdout + result.stderr
    assert "不允许的提升方向" not in result.stdout + result.stderr


def test_guard_is_read_only(repo: Path) -> None:
    """TC-I-017（REQ-015） 守卫只诊断不动手：跑完之后 ref 与工作区必须原样。

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
