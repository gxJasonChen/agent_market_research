"""提升守卫的决策内核（spec/000 §2.2–§2.5）。

用例编号：TC-U-029（方向白名单与规则判定）、TC-U-030（守卫自身既不是「永远通过」
也不是「永远失败」）。

为什么要给一个流程工具写用例：`test_architecture.py` 开头是同一个理由——写在文档里的
规矩，没有测试守着，几周之后就不成立了。这里的用例两个方向都测：**该拦的拦住**，
也**该放的放行**。只测前者的话，一个永远返回 FAIL 的守卫同样能全绿。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import check_promotion as guard
import pytest

INTEGRATION = ("bugfix", "development")
RELEASE = ("development", "main")


def _facts(key: tuple[str, str] = INTEGRATION, **overrides: Any) -> guard.Facts:
    """一份「一切正常」的事实，用例只改自己关心的那一项。"""
    edge = guard.EDGES[key]
    base = guard.Facts(
        source_ref=edge.source,
        source_sha="c3",
        target_ref=edge.target,
        target_sha="c1",
        dirty_paths=(),
        target_private_commits=(),
        target_is_ancestor_of_source=True,
        pending_commits=("c2 feat: 第二个提交", "c3 feat: 第三个提交"),
    )
    return replace(base, **overrides)


def test_only_the_two_promotion_directions_exist() -> None:
    """TC-U-029 方向白名单（spec/000 §2.2）：反向流动与跳级都不在表里。"""
    assert set(guard.EDGES) == {INTEGRATION, RELEASE}
    assert guard.find_edge("development", "bugfix") is None  # 反向
    assert guard.find_edge("main", "development") is None  # 反向
    assert guard.find_edge("bugfix", "main") is None  # 跳级


def test_clean_integration_promotion_passes() -> None:
    """TC-U-029 该放的放行：没分叉、工作区干净、有内容可提升。"""
    verdict = guard.evaluate(guard.EDGES[INTEGRATION], _facts())
    assert verdict.ok
    assert verdict.blockers == ()


def test_dirty_worktree_blocks_promotion() -> None:
    """TC-U-029 门禁只在干净工作区上成立：未提交的改动会让「被接受的 sha」名不副实。"""
    verdict = guard.evaluate(
        guard.EDGES[INTEGRATION], _facts(dirty_paths=(" M src/amr/config.py",))
    )
    assert not verdict.ok
    assert any("工作区" in blocker for blocker in verdict.blockers)


@pytest.mark.parametrize("key", [INTEGRATION, RELEASE])
def test_target_with_private_commits_blocks_promotion(key: tuple[str, str]) -> None:
    """TC-U-029 不变量 1、2 共用一条判定：目标分支不能有源分支没有的提交。

    出现了就说明已经分叉，此时「提升」不再是沿一条线前进而是一次真合并，
    历史上再也看不出哪一批被验收过——所以要拦，并且点名那个提交。
    """
    verdict = guard.evaluate(
        guard.EDGES[key], _facts(key, target_private_commits=("c9 chore: 直接提交到目标分支",))
    )
    assert not verdict.ok
    assert any("c9" in blocker for blocker in verdict.blockers)


def test_integration_edge_requires_fast_forwardable_target() -> None:
    """TC-U-029 集成边是 `--ff-only`：目标不是源的祖先就说明分叉了（spec/000 §2.2）。"""
    verdict = guard.evaluate(guard.EDGES[INTEGRATION], _facts(target_is_ancestor_of_source=False))
    assert not verdict.ok
    assert any("--ff-only" in blocker for blocker in verdict.blockers)


def test_release_edge_tolerates_target_that_is_not_ancestor() -> None:
    """TC-U-029 发布边是 `--no-ff`，这条特性必须被守住（spec/000 §2.3 的注解）。

    发布合并提交只存在于 `main` 上，所以第一次发布之后 `main` 永远不是
    `development` 的祖先。若把「`main` ⊆ `development`」当不变量，
    守卫会在每次发布后永久报假警——这正是本轮把不变量改写成
    「`main` 上没有 `bugfix` 之外的提交」的原因。
    """
    verdict = guard.evaluate(
        guard.EDGES[RELEASE],
        _facts(RELEASE, target_is_ancestor_of_source=False),
        tag="v0.1.0",
    )
    assert verdict.ok


def test_nothing_pending_is_a_note_not_a_blocker() -> None:
    """TC-U-029 没内容可提升不是错误，是提示——否则重复执行会莫名其妙地红。"""
    verdict = guard.evaluate(guard.EDGES[INTEGRATION], _facts(pending_commits=()))
    assert verdict.ok
    assert any("没有待提升" in note for note in verdict.notes)


def test_release_without_tag_warns_about_tagging() -> None:
    """TC-U-029 发布边不打 tag 就等于没有发布边界（spec/000 §2.3 不变量 3）。"""
    verdict = guard.evaluate(guard.EDGES[RELEASE], _facts(RELEASE))
    assert verdict.ok
    assert any("tag" in note for note in verdict.notes)


@pytest.mark.parametrize("tag", ["0.1.0", "v1.0", "release-1", "v1.0.0.0", "v1.0.0-rc1"])
def test_release_tag_must_be_semver(tag: str) -> None:
    """TC-U-029 版本号形如 `v0.1.0`（spec/000 §2.3 不变量 3）。"""
    verdict = guard.evaluate(guard.EDGES[RELEASE], _facts(RELEASE), tag=tag)
    assert not verdict.ok
    assert any("SemVer" in blocker for blocker in verdict.blockers)


def test_existing_tag_is_blocked() -> None:
    """TC-U-029 同一个版本号不能发两次：tag 已存在时拒绝。"""
    verdict = guard.evaluate(
        guard.EDGES[RELEASE], _facts(RELEASE), tag="v0.1.0", existing_tags=("v0.1.0",)
    )
    assert not verdict.ok
    assert any("已存在" in blocker for blocker in verdict.blockers)


def test_guard_is_neither_always_pass_nor_always_fail() -> None:
    """TC-U-030 守卫自身：同样的输入只改一处违规，结论必须真的翻转。

    一个恒真或恒假的守卫都能骗过「只测一边」的用例，所以这里把两侧放在一起断言。
    """
    edge = guard.EDGES[INTEGRATION]
    assert guard.evaluate(edge, _facts()).ok is True
    assert guard.evaluate(edge, _facts(dirty_paths=("?? 新文件",))).ok is False
