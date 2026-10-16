#!/usr/bin/env python
"""提升守卫：把 spec/000 §2 的合并规则变成会失败的检查。

用法：

    python scripts/check_promotion.py bugfix development              # 集成边（QA 执行）
    python scripts/check_promotion.py development main --tag v0.1.0  # 发布边（人执行）
    python scripts/check_promotion.py development main --run-gates --with-system

它**只读**：不合并、不推送、不改任何 ref，只给出结论、阻断点与「接下来该执行的命令」。
工具一旦顺手把合并做掉，spec/000 §2.2 的角色分工（集成边属 QA、发布边属人）就失效了。

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
    target_private_commits: tuple[str, ...]
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
) -> Verdict:
    """决定「这次提升能不能做」。纯函数：同样的输入永远给同样的结论。"""
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

    if edge.strategy == "no-ff":
        if tag is None:
            notes.append("未指定 --tag：合并后别忘了在 main 上打 SemVer tag（spec/000 §2.3）")
        elif not SEMVER.match(tag):
            blockers.append(f"版本号 {tag!r} 不符合 SemVer（形如 v0.1.0）")
        elif tag in existing_tags:
            blockers.append(f"tag {tag} 已存在，换一个版本号（不变量 3：一个版本只发一次）")

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


def collect_facts(
    repo: Path, edge: Edge, *, source_ref: str | None = None, target_ref: str | None = None
) -> Facts:
    """从 git 采集判定所需的事实；只读，不改任何 ref。"""
    source = source_ref or edge.source
    target = target_ref or edge.target

    # `--no-merges`：发布边的 `--no-ff` 合并提交只存在于 main 上，那是设计要留的痕，
    # 不是「私货」——把它们算进不变量会让每次发布之后守卫永久报假警。
    private = _lines(
        _git(
            repo,
            "log",
            "--no-merges",
            "--format=%h %s",
            target,
            "--not",
            source,
        )
    )
    pending = _lines(_git(repo, "log", "--format=%h %s", source, "--not", target))

    return Facts(
        source_ref=source,
        source_sha=_git(repo, "rev-parse", "--verify", f"{source}^{{commit}}"),
        target_ref=target,
        target_sha=_git(repo, "rev-parse", "--verify", f"{target}^{{commit}}"),
        dirty_paths=_lines(_git(repo, "status", "--porcelain")),
        target_private_commits=private,
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
        f"  目标分支私有提交：{len(facts.target_private_commits)} 个",
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

    out.append("")
    out.append(f"复现：git rev-list --no-merges {edge.target} --not {edge.source}")
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
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    edge = find_edge(branch_name(args.source), branch_name(args.target))
    if edge is None:
        allowed = "、".join(item.direction for item in EDGES.values())
        print(
            f"不允许的提升方向：{args.source} → {args.target}"
            f"（spec/000 §2.2 只允许 {allowed}）",
            file=sys.stderr,
        )
        return 1

    try:
        facts = collect_facts(
            args.repo, edge, source_ref=args.source, target_ref=args.target
        )
        existing_tags = _lines(_git(args.repo, "tag", "--list"))
    except GitError as exc:
        print(f"无法采集事实：{exc}", file=sys.stderr)
        return 1

    verdict = evaluate(edge, facts, tag=args.tag, existing_tags=existing_tags)

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
