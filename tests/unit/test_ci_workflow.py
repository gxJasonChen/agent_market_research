"""CI 工作流的触发点与依赖关系（spec/000 §2.7–§2.9）。

用例编号：TC-U-033（门禁与交棒）、TC-U-034（Codex 自动评审的接线方式）。

需求追溯：REQ-015（交付流程守卫）。

为什么要给 YAML 写用例：`spec/000` §2.7 规定不变量要在 CI 上被机械校验，而「校验真的
会被执行」这件事此前只有文档和注释在保证——`37f1104` 修的正是它从没执行过：`branch-guard`
只挂 `pull_request`，而 CI 自己开出来的 PR 由 `GITHUB_TOKEN` 触发，其运行会被扣在
`action_required` 等人工批准，一个 job 都不跑，于是 PR 的 check 列表上它永远停在 `skipping`。

那次是靠人肉读运行记录发现的。这类回归完全静默——工作流语法合法、CI 一片绿，
只有「本该跑的检查没跑」。仓库里已有同类先例（`test_crews_config.py` 直接解析 YAML、
`test_architecture.py` 扫源码），所以这里把工作流也纳入。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from amr.config import PROJECT_ROOT

WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _on(document: dict[str, Any], source: Path) -> dict[str, Any]:
    """取 `on:` 段。PyYAML 按 YAML 1.1 把裸词 `on` 解析成布尔 True，两种键都要试。"""
    for key in (True, "on"):
        if key in document:
            return document[key]
    raise AssertionError(f"{source} 里找不到 `on` 触发器")


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return _load(CI)


@pytest.fixture(scope="module")
def triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    return _on(workflow, CI)


@pytest.fixture(scope="module")
def codex_review(workflow: dict[str, Any]) -> dict[str, Any]:
    return workflow["jobs"]["codex-review"]


def test_gate_and_guard_cover_every_push_to_bugfix(triggers: dict[str, Any]) -> None:
    """TC-U-033（REQ-015） push 到 `bugfix` 是主触发点：开发 Agent 的每一次推送都要过门禁。"""
    assert triggers["push"]["branches"] == ["bugfix"]


def test_branch_guard_runs_on_push_not_only_on_pull_request(
    workflow: dict[str, Any], triggers: dict[str, Any]
) -> None:
    """TC-U-033（REQ-015） `branch-guard` 必须挂在 push 到 `bugfix` 上（spec/000 §2.7）。

    只挂 `pull_request` 是不够的：bot 开出来的 PR 其运行会被扣在 `action_required`，
    这道校验于是在自动流程里从不执行。断言的写法要挡住「有人把 push 那一半删掉」，
    所以要求 `if` 里**同时**出现两种事件名，而不是只判断它非空。
    """
    condition = workflow["jobs"]["branch-guard"]["if"]

    assert isinstance(condition, str), "`if` 必须是字符串；用 ${{ }} 包起来会让它恒为真"
    assert "pull_request" in condition
    assert "push" in condition
    assert "${{" not in condition, "`if` 里不该再有未展开的 ${{ }}"
    # 这一条是为 `needs` 的语义守的：上游被跳过，下游一起被跳过——不报错，只是静默停摆。
    # 谁要是把 branch-guard 的 push 触发面收窄，`open-pr` 就永远开不出 PR，
    # 而门禁仍然全绿。所以这里钉死它的触发面必须覆盖 open-pr 的那一段。
    assert "refs/heads/bugfix" in condition, (
        "branch-guard 的 push 触发面被收窄：open-pr 依赖它，会被连带 skip 而静默停摆"
    )


def test_open_pr_waits_for_the_branch_guard(workflow: dict[str, Any]) -> None:
    """TC-U-033（REQ-015） 不变量红了就不该自动开 PR 去请求 review（spec/000 §2.7）。

    少了这条依赖，「检查会失败」与「失败了会阻断」就是两回事：分叉照样开 PR 并 @Codex，
    机械校验沦为装饰。
    """
    needs = workflow["jobs"]["open-pr"]["needs"]
    listed = [needs] if isinstance(needs, str) else list(needs)

    assert "gate" in listed
    assert "branch-guard" in listed


def test_pull_request_trigger_covers_both_promotion_targets(
    triggers: dict[str, Any],
) -> None:
    """TC-U-033（REQ-015） PR 侧要覆盖两条提升边的目标分支（§2.2 只允许 `development` 与 `main`）。"""
    assert set(triggers["pull_request"]["branches"]) == {"development", "main"}


def test_codex_review_is_not_wired_to_either_silent_failure(
    triggers: dict[str, Any],
) -> None:
    """TC-U-034（REQ-015） 自动评审不能挂 `issue_comment`，也不能挂 `workflow_run`。

    两个坑都是**实测**踩出来的，都会静默失效——没有报错、check 全绿，唯一的症状是
    「Codex 从不出现」，而症状本身没有任何地方会报错：

    - `issue_comment`：`open-pr` 那句 `@codex` 是 `GITHUB_TOKEN` 发的，而 `GITHUB_TOKEN`
      产生的事件**不会启动新的 workflow run**（防递归）。监听它等于监听一个永不触发的事件。
    - `workflow_run`：要求 workflow 文件**存在于默认分支**上。本仓库 `main` 只接受人工
      发布，所以单独挂它要等跟完整圈才生效；在那之前 GitHub 只会给它一个**名为文件路径的
      失败运行**（无 job、`event=push`、提示 "workflow file issue"），而文件本身完全合法
      ——用一个 8 行的最小对照 workflow 实测确认过同样的症状。

    所以评审并进 `ci.yml` 跟着 push 触发：push 事件用的是被推送的那个版本的 workflow
    文件，推上去立刻生效，不用等它上默认分支。
    """
    assert "issue_comment" not in triggers

    offenders = [
        path.name
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
        if "workflow_run" in _on(_load(path), path)
    ]
    assert offenders == [], (
        f"{offenders} 挂了 workflow_run：它要求文件在默认分支上，否则永远不会执行。"
        f"要么并进 ci.yml，要么先把它送到 main。"
    )


def test_codex_review_only_runs_on_push_to_bugfix(codex_review: dict[str, Any]) -> None:
    """TC-U-034（REQ-015） 评审只在 push 到 `bugfix` 时跑，而且要等门禁绿。

    门禁红的时候不该请人评审——先让门禁说了算，也不白烧 token。
    """
    condition = codex_review["if"]

    assert isinstance(condition, str)
    assert "github.event_name == 'push'" in condition
    assert "refs/heads/bugfix" in condition
    assert "${{" not in condition, "`if` 里不该有未展开的 ${{ }}"

    needs = codex_review["needs"]
    listed = [needs] if isinstance(needs, str) else list(needs)
    assert "gate" in listed
    assert "branch-guard" in listed


def test_codex_review_cannot_write_contents(codex_review: dict[str, Any]) -> None:
    """TC-U-034（REQ-015） 自动评审没有内容写权限——**自动合并的授权不在这里**。

    合并 `development` 是 QA（Codex）的决定，要人明确点头之后才加。把这条断言写下来，
    是为了让「顺手把 contents 提成 write，好让它自动合并」这个改动必须先让测试红一次，
    而不是在某个深夜悄悄发生。
    """
    permissions = codex_review["permissions"]

    assert permissions.get("contents") == "read"
    assert permissions.get("pull-requests") == "write"


def test_no_workflow_uses_expression_syntax_as_a_literal() -> None:
    """TC-U-034（REQ-015） 任何 workflow 都不许在 `run` 块里出现表达式语法的字面量。

    `run:` 的脚本内容会被 GitHub 扫描 `${{ … }}` 表达式，而**空表达式直接让整个
    workflow 校验失败**：运行零 job、只提示 "This run likely failed because of a
    workflow file issue"，**而文件本身 YAML 解析完全正常**——逐键核对过，本地门禁
    （lint / type / test）也全绿。这个坑真踩过：`ci.yml` 因此红了一次，把门禁本身
    挡在门外；`codex-qa.yml` 那次失败是它与「`workflow_run` 要求默认分支」两个问题
    叠加，探针只证明了后者。

    典型诱因是在注释里举例说明「别用表达式语法」——**举例本身就成了非法表达式**。
    这类失效本地一条都测不出来，只有推上去才会以「零 job 的失败运行」出现。
    """
    contexts = r"\s*(github|secrets|vars|env|needs|steps|runner|inputs|matrix|job|jobs)\b"
    offenders: list[str] = []

    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        document = _load(path)
        for job_id, job in (document.get("jobs") or {}).items():
            for index, step in enumerate(job.get("steps") or []):
                for expression in re.findall(r"\$\{\{(.*?)\}\}", str(step.get("run", "")), re.S):
                    if not expression.strip():
                        offenders.append(f"{path.name} {job_id}.steps[{index}]：空表达式")
                    elif not re.match(contexts, expression):
                        offenders.append(
                            f"{path.name} {job_id}.steps[{index}]：可疑表达式 {expression.strip()!r}"
                        )

    assert offenders == [], f"run 块里有非法的表达式字面量：{offenders}"


def test_codex_review_runs_read_only_with_a_timeout(codex_review: dict[str, Any]) -> None:
    """TC-U-034（REQ-015） 评审跑在只读沙箱里，且带超时刹车。

    评审是只读活动，给它写权限没有任何正当理由；而这是按 token 计费的，
    没有 `timeout-minutes` 就等于没有刹车。
    """
    script = "\n".join(str(step.get("run", "")) for step in codex_review["steps"])

    assert "-s read-only" in script
    assert "--dangerously-bypass-approvals-and-sandbox" not in script
    assert codex_review.get("timeout-minutes")
