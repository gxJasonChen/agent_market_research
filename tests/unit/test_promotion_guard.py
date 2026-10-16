"""提升守卫的决策内核（spec/000 §2.2–§2.5）。

用例编号：TC-U-031（方向白名单与规则判定）、TC-U-032（守卫自身既不是「永远通过」
也不是「永远失败」，以及开关语义）。

需求追溯：REQ-015（交付流程守卫）。

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
        release_merges=(),
        misplaced_tags=(),
        target_is_ancestor_of_source=True,
        pending_commits=("c2 feat: 第二个提交", "c3 feat: 第三个提交"),
    )
    return replace(base, **overrides)


def test_only_the_two_promotion_directions_exist() -> None:
    """TC-U-031 方向白名单（spec/000 §2.2）：反向流动与跳级都不在表里。"""
    assert set(guard.EDGES) == {INTEGRATION, RELEASE}
    assert guard.find_edge("development", "bugfix") is None  # 反向
    assert guard.find_edge("main", "development") is None  # 反向
    assert guard.find_edge("bugfix", "main") is None  # 跳级


def test_clean_integration_promotion_passes() -> None:
    """TC-U-031 该放的放行：没分叉、工作区干净、有内容可提升。"""
    verdict = guard.evaluate(guard.EDGES[INTEGRATION], _facts())
    assert verdict.ok
    assert verdict.blockers == ()


def test_dirty_worktree_blocks_promotion() -> None:
    """TC-U-031 门禁只在干净工作区上成立：未提交的改动会让「被接受的 sha」名不副实。"""
    verdict = guard.evaluate(
        guard.EDGES[INTEGRATION], _facts(dirty_paths=(" M src/amr/config.py",))
    )
    assert not verdict.ok
    assert any("工作区" in blocker for blocker in verdict.blockers)


@pytest.mark.parametrize("key", [INTEGRATION, RELEASE])
def test_target_with_private_commits_blocks_promotion(key: tuple[str, str]) -> None:
    """TC-U-031 不变量 1、2 共用一条判定：目标分支不能有源分支没有的提交。

    出现了就说明已经分叉，此时「提升」不再是沿一条线前进而是一次真合并，
    历史上再也看不出哪一批被验收过——所以要拦，并且点名那个提交。
    """
    verdict = guard.evaluate(
        guard.EDGES[key], _facts(key, target_private_commits=("c9 chore: 直接提交到目标分支",))
    )
    assert not verdict.ok
    assert any("c9" in blocker for blocker in verdict.blockers)


def test_untagged_merge_on_target_is_private() -> None:
    """TC-U-031 不变量 2 的判别依据是「带 tag 的发布合并提交」，不是「合并提交」。

    把已经验收过的 `bugfix` 直接 `--no-ff` 合进 `main`，会留下一个合并提交：
    它带进来的内容都在 `development` 上，于是没有任何**非合并**提交露出破绽。
    采集层若用 `--no-merges` 一刀切，这次合并就彻底隐形，守卫对着分叉报「允许提升」。
    [H1] 这条用例钉住判定层：这类合并必须落进 `target_private_commits`。
    """
    verdict = guard.evaluate(
        guard.EDGES[RELEASE],
        _facts(
            RELEASE,
            target_private_commits=("c9 chore: 直接把 bugfix 合进 main（未经发布边）",),
        ),
        tag="v0.2.0",
    )
    assert not verdict.ok
    assert any("c9" in blocker for blocker in verdict.blockers)


def test_release_merges_are_a_note_not_a_blocker() -> None:
    """TC-U-031 合法的发布合并提交要放行，并在证据块里点名。

    发布边用 `--no-ff`，`main` 上的合并提交**不在** `development` 上——把它算作私货
    会让每次发布之后守卫永久报假警；把它整个滤掉又会放过绕过发布边的合并。所以是
    按「合并提交 + 带 tag + 第二父在源分支上」精确识别，识别出来的记成提示。
    """
    verdict = guard.evaluate(
        guard.EDGES[RELEASE],
        _facts(RELEASE, release_merges=("c8 release: v0.1.0（tag: v0.1.0）",)),
        tag="v0.2.0",
    )
    assert verdict.ok
    assert any("发布合并提交" in note for note in verdict.notes)


def test_integration_edge_requires_fast_forwardable_target() -> None:
    """TC-U-031 集成边是 `--ff-only`：目标不是源的祖先就说明分叉了（spec/000 §2.2）。"""
    verdict = guard.evaluate(guard.EDGES[INTEGRATION], _facts(target_is_ancestor_of_source=False))
    assert not verdict.ok
    assert any("--ff-only" in blocker for blocker in verdict.blockers)


def test_release_edge_tolerates_target_that_is_not_ancestor() -> None:
    """TC-U-031 发布边是 `--no-ff`，这条特性必须被守住（spec/000 §2.3 的注解）。

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
    """TC-U-031 没内容可提升不是错误，是提示——否则重复执行会莫名其妙地红。"""
    verdict = guard.evaluate(guard.EDGES[INTEGRATION], _facts(pending_commits=()))
    assert verdict.ok
    assert any("没有待提升" in note for note in verdict.notes)


def test_release_without_tag_blocks_promotion() -> None:
    """TC-U-031 发布边不给 `--tag` 是阻断点，不是提示（spec/000 §2.5）。

    原本降级成一句提示，于是「版本号合法」与「版本没发过」两道门禁在不给 `--tag`
    时静默变成不检查——而 CI 的两个触发点都从不传 `--tag`，等于发布边的 tag 门禁
    在 CI 上永远不执行。缺参数时 fail-open 比直接报错危险得多。
    """
    verdict = guard.evaluate(guard.EDGES[RELEASE], _facts(RELEASE))
    assert not verdict.ok
    assert any("--tag" in blocker for blocker in verdict.blockers)


def test_tag_on_integration_edge_blocks_promotion() -> None:
    """TC-U-031 集成边不接受 `--tag`：静默忽略会让传参的人以为它生效了。"""
    verdict = guard.evaluate(guard.EDGES[INTEGRATION], _facts(), tag="v9.9.9")
    assert not verdict.ok
    assert any("--tag" in blocker for blocker in verdict.blockers)


def test_misplaced_version_tag_blocks_promotion() -> None:
    """TC-U-031 不变量 3 的后半句「tag 只打在 `main` 上」要有机械校验。

    原本 tag 在实现里只以**名字**出现过：只查格式与重名，从不看它指向哪个提交，
    于是「tag 打在 bugfix 上」零覆盖。这条不变量是全局的，与本次走哪条边无关。
    """
    verdict = guard.evaluate(
        guard.EDGES[INTEGRATION], _facts(misplaced_tags=("v0.5.0 → abc123def456（不在 main 上）",))
    )
    assert not verdict.ok
    assert any("没打在 main 上" in blocker for blocker in verdict.blockers)


@pytest.mark.parametrize("tag", ["0.1.0", "v1.0", "release-1", "v1.0.0.0", "v1.0.0-rc1"])
def test_release_tag_must_be_semver(tag: str) -> None:
    """TC-U-031 版本号形如 `v0.1.0`（spec/000 §2.3 不变量 3）。"""
    verdict = guard.evaluate(guard.EDGES[RELEASE], _facts(RELEASE), tag=tag)
    assert not verdict.ok
    assert any("SemVer" in blocker for blocker in verdict.blockers)


def test_existing_tag_is_blocked() -> None:
    """TC-U-031 同一个版本号不能发两次：tag 已存在时拒绝。"""
    verdict = guard.evaluate(
        guard.EDGES[RELEASE], _facts(RELEASE), tag="v0.1.0", existing_tags=("v0.1.0",)
    )
    assert not verdict.ok
    assert any("已存在" in blocker for blocker in verdict.blockers)


def test_guard_is_neither_always_pass_nor_always_fail() -> None:
    """TC-U-032 守卫自身：同样的输入只改一处违规，结论必须真的翻转。

    一个恒真或恒假的守卫都能骗过「只测一边」的用例，所以这里把两侧放在一起断言。
    """
    edge = guard.EDGES[INTEGRATION]
    assert guard.evaluate(edge, _facts()).ok is True
    assert guard.evaluate(edge, _facts(dirty_paths=("?? 新文件",))).ok is False


def test_with_system_implies_run_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """TC-U-032 `--with-system` 单独给时必须真的跑门禁。

    它原本只在 `--run-gates` 也给了的时候才拼接 `make test-system`，单独给则静默
    什么都不跑、也不报错——而「系统测试通过」在 §2.5 里是硬门禁，写这个开关的人
    会以为它跑过了。这里断言开关的规范化，不真跑 `make`。
    """
    called: list[bool] = []
    monkeypatch.setattr(
        guard, "run_gates", lambda _repo, *, with_system: (called.append(with_system), ())[1]
    )
    monkeypatch.setattr(guard, "evaluate", lambda *_a, **_kw: guard.Verdict(ok=True))
    monkeypatch.setattr(guard, "collect_facts", lambda *_a, **_kw: _facts(INTEGRATION))
    monkeypatch.setattr(guard, "_git", lambda *_a, **_kw: "")

    guard.main(["bugfix", "development", "--with-system"])

    assert called == [True]


def test_without_gate_flags_nothing_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """TC-U-032 两个门禁开关都不给时不该跑测试——门禁是显式 opt-in。"""
    called: list[bool] = []
    monkeypatch.setattr(
        guard, "run_gates", lambda _repo, *, with_system: (called.append(with_system), ())[1]
    )
    monkeypatch.setattr(guard, "evaluate", lambda *_a, **_kw: guard.Verdict(ok=True))
    monkeypatch.setattr(guard, "collect_facts", lambda *_a, **_kw: _facts(INTEGRATION))
    monkeypatch.setattr(guard, "_git", lambda *_a, **_kw: "")

    guard.main(["bugfix", "development"])

    assert called == []
