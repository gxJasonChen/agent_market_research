#!/usr/bin/env python
"""提升守卫：把 spec/000 §2 的合并规则变成会失败的检查。

用法：

    python scripts/check_promotion.py bugfix development              # 集成边（QA 执行）
    python scripts/check_promotion.py development main --tag v0.1.0  # 发布边（人执行）
    python scripts/check_promotion.py development main --run-gates --with-system

它**只读**：不合并、不推送、不改任何 ref，只给出结论、阻断点与「接下来该执行的命令」。
工具一旦顺手把合并做掉，spec/000 §2.2 的角色分工（集成边属 QA、发布边属人）就失效了。

已知边界（写在这里而不是藏起来）：`--tag` 的「版本没发过」只比对**本地** tag
（`git tag --list`）。别人在远端建的 tag、本地又没 `git fetch --tags` 时看不出来，
要等 `git push --tags` 才炸。守卫不联网是有意的——一个只读的本地工具不该为了这条
边角检查去访问远端；发布前先 `git fetch --prune --tags origin` 即可。

判定逻辑（`evaluate`）是纯函数，由 `tests/unit/test_promotion_guard.py` 覆盖；
git 采集（`collect_facts`）由 `tests/integration/test_promotion_guard.py` 在真实仓库上覆盖。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 版本号形如 `v0.1.0`（spec/000 §2.3 不变量 3）。预发布号暂不支持：发布边只出正式版本。
SEMVER = re.compile(r"^v\d+\.\d+\.\d+$")

#: 证据块里每条清单最多列几条，避免长批次把结论冲掉
PREVIEW_LIMIT = 5


class GitError(RuntimeError):
    """git 命令失败。把 stderr 带出来，避免「静默失败」被当成通过。"""


@dataclass(frozen=True, slots=True)
class Edge:
    """一条允许的提升边（spec/000 §2.2）。"""

    source: str
    target: str
    strategy: Literal["ff-only", "no-ff"]
    executor: str
    checklist: str

    @property
    def direction(self) -> str:
        return f"{self.source} → {self.target}"


#: 只允许这两条边。反向流动（main→development、development→bugfix）与跳级（bugfix→main）都不在表里。
EDGES: dict[tuple[str, str], Edge] = {
    ("bugfix", "development"): Edge(
        source="bugfix",
        target="development",
        strategy="ff-only",
        executor="Codex（QA 通过后）",
        checklist="spec/000 §2.4",
    ),
    ("development", "main"): Edge(
        source="development",
        target="main",
        strategy="no-ff",
        executor="人（人工合并）",
        checklist="spec/000 §2.5",
    ),
}


def find_edge(source: str, target: str) -> Edge | None:
    """按分支名查边；不在白名单里就返回 None（调用方负责拒绝）。"""
    return EDGES.get((source, target))


def branch_name(ref: str) -> str:
    """允许传 `origin/bugfix` 这类远程跟踪引用：查方向时只比对分支名。"""
    return ref.removeprefix("origin/")


# --------------------------------------------------------------------------- 决策内核


@dataclass(frozen=True, slots=True)
class Facts:
    """判定一次提升所需的全部事实。

    由 git 采集，也可以在用例里直接构造——判定逻辑因此不需要真实仓库就能被覆盖。
    """

    source_ref: str
    source_sha: str
    target_ref: str
    target_sha: str
    dirty_paths: tuple[str, ...]
    #: 目标分支上「源分支没有」的提交里，**不算发布合并提交**的那些（私货）。
    target_private_commits: tuple[str, ...]
    #: 同上集合里被认定为合法发布合并提交的那些，只用来在证据块里展示。
    release_merges: tuple[str, ...]
    #: SemVer 版本 tag 里指向 `main` 之外的（不变量 3 的后半句）。全局不变量，与边无关。
    misplaced_tags: tuple[str, ...]
    target_is_ancestor_of_source: bool
    pending_commits: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Verdict:
    ok: bool
    blockers: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def evaluate(
    edge: Edge,
    facts: Facts,
    *,
    tag: str | None = None,
    existing_tags: tuple[str, ...] = (),
    audit: bool = False,
) -> Verdict:
    """决定「这次提升能不能做」。纯函数：同样的输入永远给同样的结论。

    `audit=True` 是「只核对不变量，不声明本次发布的版本」：跳过版本号相关的门禁。
    把「审计不变量」与「宣布发哪个版本」压成同一个入口，会让前者被后者连带否决——
    CI 上 `development → main` 的 PR 无从知道下一个版本号，那条 check 就永远是红的。
    """
    blockers: list[str] = []
    notes: list[str] = []

    if facts.dirty_paths:
        blockers.append(
            f"工作区不干净（{len(facts.dirty_paths)} 个未提交路径），"
            f"先提交或收纳再提升：{facts.dirty_paths[0]}"
        )

    # 不变量 1、2 的共同形式：目标分支上不能有源分支没有的提交。
    # 出现这类提交就说明已经分叉（有人越过提升边直接写目标分支），
    # 此时「合并」不再是沿一条线前进，历史上再也看不出哪一批被验收过。
    #
    # 唯一的例外是发布合并提交（`--no-ff` 把 development 合进 main），它是设计要留的痕，
    # 由 collect_facts 单独识别并放进 release_merges。识别它必须**同时**满足「是合并提交」
    # 与「带 tag」：少了 tag 这一条，一次绕过发布边的合并（把已经验收过的 bugfix 直接
    # --no-ff 合进 main）会和正经发布长得一模一样——它带进来的内容都在 development 上，
    # 没有任何非合并提交会露出破绽。曾经用 `--no-merges` 一刀切，正是栽在这里。
    if facts.target_private_commits:
        listed = "\n".join(f"      {item}" for item in facts.target_private_commits)
        blockers.append(
            f"{facts.target_ref} 上有 {facts.source_ref} 没有的提交（{len(facts.target_private_commits)} 个），"
            f"已经分叉——按 spec/000 §2.3 停下来查因，不要改用普通合并把它盖过去：\n{listed}"
        )

    if edge.strategy == "ff-only" and not facts.target_is_ancestor_of_source:
        blockers.append(
            f"{facts.target_ref} 不是 {facts.source_ref} 的祖先，--ff-only 快进不可用"
        )

    if not facts.pending_commits:
        notes.append(
            f"没有待提升的提交（{facts.target_ref} 与 {facts.source_ref} 内容一致），本次不需要合并"
        )

    # 集成边是 --ff-only，没有版本边界可言。静默忽略会让「我传了 --tag」看起来生效了。
    if edge.strategy != "no-ff" and tag is not None:
        blockers.append(
            f"--tag 只对发布边有意义：{edge.direction} 是 --ff-only，不接受版本号"
        )

    if edge.strategy == "no-ff" and not audit:
        if tag is None:
            # 不能降级成提示：缺了 --tag，「版本号合法」与「版本没发过」两道门禁（§2.5）
            # 会静默变成不检查，而调用方（尤其是 CI）无从察觉。
            blockers.append(
                "发布边必须指定 --tag（spec/000 §2.5：版本号 SemVer，且该 tag 尚不存在）；"
                "缺了它，版本号相关的门禁会变成不检查。只想核对不变量而不发版，用 --audit"
            )
        elif not SEMVER.match(tag):
            blockers.append(f"版本号 {tag!r} 不符合 SemVer（形如 v0.1.0）")
        elif tag in existing_tags:
            blockers.append(f"tag {tag} 已存在，换一个版本号（不变量 3：一个版本只发一次）")

    # 不变量 3 的后半句「tag 只打在 main 上」是全局的，与本次走哪条边无关。
    # 条目既可能是错位的 tag，也可能是「定位不到 main」这条 fail-closed 说明。
    if facts.misplaced_tags:
        listed = "\n".join(f"      {item}" for item in facts.misplaced_tags)
        blockers.append(f"不变量 3（版本 tag 只打在 main 上）未通过：\n{listed}")

    if facts.release_merges:
        notes.append(
            f"{facts.target_ref} 上有 {len(facts.release_merges)} 个带 tag 的发布合并提交，"
            f"按 spec/000 §2.2 放行"
        )

    return Verdict(ok=not blockers, blockers=tuple(blockers), notes=tuple(notes))


# --------------------------------------------------------------------------- git 采集


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"退出码 {result.returncode}"
        raise GitError(f"git {' '.join(args)} 失败：{message}")
    return result.stdout.strip()


def _lines(text: str) -> tuple[str, ...]:
    return tuple(line for line in text.splitlines() if line.strip())


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """`git merge-base --is-ancestor` 用退出码说话：0 是祖先，1 不是，其他是出错。"""
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise GitError(f"git merge-base 失败：{result.stderr.strip()}")
    return result.returncode == 0


def _tagged_commits(repo: Path) -> tuple[tuple[str, str], ...]:
    """(tag 名, 它指向的提交)。**只收能解成 commit 的 tag。**

    两条理由，都不是假想：
    - tag 可以合法地指向 tree/blob（`git tag v9 <tree-sha>`）。那种 tag 送到
      `merge-base --is-ancestor` 会以 128 报错而不是回 1，`_is_ancestor` 于是抛
      `GitError`，守卫**对所有边**一起失效——连与 tag 毫无关系的集成边自检都跑不了，
      报文还完全指不到成因。
    - 用 `^{commit}` 解引用而不是自己剥 `%(objectname)`：附注 tag 指向另一个 tag 时
      要递归剥，分隔符解析也就不用操心 tag 名里有没有空格（`git check-ref-format`
      不允许 refname 含空格，但没必要把自己的健壮性押在这条上）。
    """
    out: list[tuple[str, str]] = []
    for name in _lines(_git(repo, "tag", "--list")):
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{name}^{{commit}}"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            out.append((name, result.stdout.strip()))
    return tuple(out)


def _release_refs(repo: Path) -> tuple[str, ...]:
    """不变量 3 里说的 `main`——**所有**能定位到的候选。

    取多个而不是「先命中谁算谁」：本地 `main` 完全可能陈旧（别人在远端发完版、
    这台机器只 `git fetch --tags`），拿陈旧的本地 `main` 当唯一判据会把一个正当的
    版本 tag 报成「没打在 main 上」。判据是全局不变量，就不该让一个可能过时的引用
    压过权威引用，也不该在两者不一致时静默取本地。
    """
    out: list[str] = []
    for candidate in ("origin/main", "main"):
        if _ref_exists(repo, candidate):
            out.append(candidate)
    return tuple(out)


def _classify_foreign(
    repo: Path, target: str, source: str, tagged: dict[str, tuple[str, ...]]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """把「目标分支有、源分支没有」的提交分成（私货，发布合并提交）。

    这里**不能**加 `--no-merges`。加了之后，一次绕过发布边的合并——把已经验收过的
    `bugfix` 直接 `--no-ff` 合进 `main`——会彻底隐形：它带进来的内容都在 `development`
    上，于是没有任何非合并提交会落进这个集合，守卫对着分叉报「允许提升」。

    判别依据是「合并提交 + **SemVer 版本 tag** + 第二父在源分支上」，三条缺一不可：

    - 少了「合并提交」，正经发布会被当成私货，每次发完版守卫永久报假警；
    - 少了第二父那一条，把任意一条旁支 `--no-ff` 合进 `main` 也算发布；
    - 少了 **SemVer** 那一条（只要求「挂了 tag」），绕过发布边之后再补一句
      `git tag wip` 就能让守卫**主动背书**那次合并——它会把该合并渲染进
      「放行的发布合并提交」并打印「按 §2.2 放行」。这正是 §2.3 写「带**版本** tag」
      而不是「带 tag」的原因，白名单的判据必须与不变量 3 的管辖范围一致。
    """
    rows = _lines(_git(repo, "log", "--format=%H%x09%P%x09%s", target, "--not", source))
    private: list[str] = []
    releases: list[str] = []
    for row in rows:
        fields = row.split("\t", 2)
        if len(fields) != 3:
            continue
        sha, parents_raw, subject = fields
        parents = parents_raw.split()
        names = tagged.get(sha, ())
        versions = tuple(name for name in names if SEMVER.match(name))
        is_release = (
            len(parents) >= 2 and bool(versions) and _is_ancestor(repo, parents[1], source)
        )
        label = f"{sha[:12]} {subject}"
        if names:
            label += f"（tag: {', '.join(names)}）"
        (releases if is_release else private).append(label)
    return tuple(private), tuple(releases)


def _misplaced_tags(
    repo: Path, tagged: tuple[tuple[str, str], ...], release_refs: tuple[str, ...]
) -> tuple[str, ...]:
    """不变量 3 的后半句：版本 tag 只打在 `main` 上。

    只查 SemVer 形态的 tag——别的名字（工具打的、临时的）不在规格管辖范围内。
    只要 tag 落在**任一**候选 `main` 上就算合规（见 `_release_refs`）。
    """
    if not release_refs:
        # fail-closed：定位不到 `main` 就说出来。静默返回空等于「不变量 3 不检查」，
        # 而这次改动刚刚把「缺东西就降级成不检查」定性为比直接报错危险得多——
        # 同一份实现里不能既这么要求别人、又给自己留一个静默分支。
        return ("定位不到 main（本地与 origin/main 都没有），不变量 3 本次未校验；先取到 main 再跑",)
    out: list[str] = []
    for name, sha in tagged:
        if not SEMVER.match(name):
            continue
        if not any(_is_ancestor(repo, sha, ref) for ref in release_refs):
            out.append(f"{name} → {sha[:12]}（不在 {'/'.join(release_refs)} 上）")
    return tuple(out)


def collect_facts(
    repo: Path, edge: Edge, *, source_ref: str | None = None, target_ref: str | None = None
) -> Facts:
    """从 git 采集判定所需的事实；只读，不改任何 ref。"""
    source = source_ref or edge.source
    target = target_ref or edge.target

    tagged_rows = _tagged_commits(repo)
    tagged: dict[str, tuple[str, ...]] = {}
    for name, sha in tagged_rows:
        tagged[sha] = (*tagged.get(sha, ()), name)

    private, releases = _classify_foreign(repo, target, source, tagged)
    pending = _lines(_git(repo, "log", "--format=%h %s", source, "--not", target))

    return Facts(
        source_ref=source,
        source_sha=_git(repo, "rev-parse", "--verify", f"{source}^{{commit}}"),
        target_ref=target,
        target_sha=_git(repo, "rev-parse", "--verify", f"{target}^{{commit}}"),
        dirty_paths=_lines(_git(repo, "status", "--porcelain")),
        target_private_commits=private,
        release_merges=releases,
        misplaced_tags=_misplaced_tags(repo, tagged_rows, _release_refs(repo)),
        target_is_ancestor_of_source=_is_ancestor(repo, target, source),
        pending_commits=pending,
    )


# --------------------------------------------------------------------------- 输出与门禁


def _preview(lines: tuple[str, ...]) -> list[str]:
    shown = list(lines[:PREVIEW_LIMIT])
    if len(lines) > PREVIEW_LIMIT:
        shown.append(f"…（其余 {len(lines) - PREVIEW_LIMIT} 条）")
    return shown


def render(
    edge: Edge,
    facts: Facts,
    verdict: Verdict,
    *,
    tag: str | None,
    gate_results: Sequence[tuple[str, bool]] = (),
) -> str:
    """输出一段可直接粘进 PR 的证据块（ref、sha、计数、结论、复现方式）。"""
    out: list[str] = [
        f"提升检查：{edge.direction}（--{edge.strategy}，{edge.checklist}）",
        "",
        f"  {facts.source_ref:<12} = {facts.source_sha[:12]}  （{len(facts.pending_commits)} 个待提升提交）",
        f"  {facts.target_ref:<12} = {facts.target_sha[:12]}",
        f"  目标分支私有提交：{len(facts.target_private_commits)} 个"
        f"（另有 {len(facts.release_merges)} 个带 tag 的发布合并提交）",
        f"  可快进：{'是' if facts.target_is_ancestor_of_source else '否'}",
        "",
    ]

    if facts.pending_commits:
        out.append("待提升：")
        out.extend(f"  {item}" for item in _preview(facts.pending_commits))
        out.append("")

    out.append(f"结论：{'允许提升 ✅' if verdict.ok else '不允许提升 ❌'}")

    if verdict.blockers:
        out.append("")
        out.append("阻断点：")
        out.extend(f"  - {blocker}" for blocker in verdict.blockers)

    if verdict.notes:
        out.append("")
        out.append("提示：")
        out.extend(f"  - {note}" for note in verdict.notes)

    if gate_results:
        out.append("")
        out.append("门禁：")
        out.extend(f"  {name}：{'通过' if ok else '未通过'}" for name, ok in gate_results)

    if verdict.ok:
        out.append("")
        out.append(f"接下来（执行人：{edge.executor}）：")
        out.extend(f"  {command}" for command in _promotion_commands(edge, tag))

    if facts.release_merges:
        out.append("")
        out.append("放行的发布合并提交：")
        out.extend(f"  {item}" for item in _preview(facts.release_merges))

    out.append("")
    # 不带 `--no-merges`：这正是判定用的那条命令，照着它核对才不会漏掉合并提交。
    out.append(f"复现：git log --format='%h %p %s' {edge.target} --not {edge.source}")
    return "\n".join(out)


def _promotion_commands(edge: Edge, tag: str | None) -> list[str]:
    """只打印命令，不执行——合并权在 spec/000 §2.2 里是分角色的。"""
    source = f"origin/{edge.source}"
    if edge.strategy == "ff-only":
        merge = f"git merge --ff-only {source}"
    else:
        version = tag or "vX.Y.Z"
        merge = f'git merge --no-ff {source} -m "release: {version}"'

    commands = [f"git checkout {edge.target}", merge]
    if edge.strategy == "no-ff":
        version = tag or "vX.Y.Z"
        commands.append(f'git tag -a {version} -m "{version}"')
        commands.append(f"git push origin {edge.target} --tags")
    else:
        commands.append(f"git push origin {edge.target}")
    return commands


def run_gates(repo: Path, *, with_system: bool) -> tuple[tuple[str, bool], ...]:
    """按顺序跑本地门禁（spec/000 §3）。输出直接透传，保证失败时能看到原因。"""
    commands = [
        ("make lint", ["make", "lint"]),
        ("make type", ["make", "type"]),
        ("make test", ["make", "test"]),
    ]
    if with_system:
        commands.append(("make test-system", ["make", "test-system"]))

    results: list[tuple[str, bool]] = []
    for name, command in commands:
        print(f"\n$ {' '.join(command)}", flush=True)
        ok = subprocess.run(command, cwd=repo, check=False).returncode == 0
        print(f"  → {name}：{'通过' if ok else '未通过'}", flush=True)
        results.append((name, ok))
    return tuple(results)


# --------------------------------------------------------------------------- CLI


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="check_promotion",
        description="校验 spec/000 §2 的提升规则；只读，不合并不推送。",
    )
    parser.add_argument("source", help="源分支，如 bugfix（可写 origin/bugfix）")
    parser.add_argument("target", help="目标分支，如 development")
    parser.add_argument(
        "--repo", type=Path, default=REPO_ROOT, help=f"仓库根目录（默认 {REPO_ROOT}）"
    )
    parser.add_argument("--tag", default=None, help="发布边的版本号，形如 v0.1.0")
    parser.add_argument(
        "--run-gates", action="store_true", help="额外跑 make lint / type / test（spec/000 §3）"
    )
    parser.add_argument(
        "--with-system", action="store_true", help="连同 make test-system 一起跑（需真实中间件）"
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="只核对不变量，不声明本次发布的版本（跳过 --tag 相关门禁；与 --tag 互斥）",
    )
    args = parser.parse_args(argv)
    if args.audit and args.tag is not None:
        parser.error("--audit 与 --tag 互斥：审计不声明版本，发版必须给版本号")
    return args


def _ref_exists(repo: Path, ref: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    # `--with-system` 隐含 `--run-gates`。单独给时原本会静默地什么都不跑，
    # 而「系统测试通过」在 §2.5 里是硬门禁——写了这个开关的人会以为它跑过了。
    if args.with_system:
        args.run_gates = True

    edge = find_edge(branch_name(args.source), branch_name(args.target))
    if edge is None:
        # 先把「分支名不认识」和「方向不允许」分开：否则打错一个字母得到的是
        # 「不允许的提升方向」，把人引去读 §2.2，而真正的问题只是拼写。
        unknown = [ref for ref in (args.source, args.target) if not _ref_exists(args.repo, ref)]
        if unknown:
            print(f"分支不存在：{'、'.join(unknown)}", file=sys.stderr)
        else:
            allowed = "、".join(item.direction for item in EDGES.values())
            print(
                f"不允许的提升方向：{args.source} → {args.target}"
                f"（spec/000 §2.2 只允许 {allowed}）",
                file=sys.stderr,
            )
        return 1

    # 目标分支还不存在不是「违规」，是「还没有东西可校验」——新仓库第一次 push 到
    # `bugfix` 就是这状态，而 ci.yml 里专门有一步负责创建 `development`。
    # 这里若退 1，依赖它的 `open-pr` 会被连带 skip，那一步永远到不了，bootstrap 死锁。
    if not _ref_exists(args.repo, args.target):
        print(
            f"目标分支 {args.target} 还不存在，没有可校验的提升（首次推送的正常状态）",
            file=sys.stderr,
        )
        return 0

    try:
        facts = collect_facts(
            args.repo, edge, source_ref=args.source, target_ref=args.target
        )
        existing_tags = _lines(_git(args.repo, "tag", "--list"))
    except GitError as exc:
        print(f"无法采集事实：{exc}", file=sys.stderr)
        return 1

    verdict = evaluate(
        edge, facts, tag=args.tag, existing_tags=existing_tags, audit=args.audit
    )

    gate_results: tuple[tuple[str, bool], ...] = ()
    if args.run_gates and verdict.ok:
        gate_results = run_gates(args.repo, with_system=args.with_system)
    elif args.run_gates:
        print("已跳过门禁：先把上面的阻断点解决（spec/000 §3：门禁红时绝不合并）。")

    print(render(edge, facts, verdict, tag=args.tag, gate_results=gate_results))

    gates_ok = all(ok for _, ok in gate_results)
    return 0 if verdict.ok and gates_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
