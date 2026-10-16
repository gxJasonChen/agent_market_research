"""CI 工作流的触发点与依赖关系（spec/000 §2.7–§2.9）。

用例编号：TC-U-033（`ci.yml` 的门禁与交棒）、TC-U-034（`codex-qa.yml` 的自动评审）。

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

from pathlib import Path
from typing import Any

import pytest
import yaml

from amr.config import PROJECT_ROOT

WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
CODEX_QA = WORKFLOWS / "codex-qa.yml"


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
def codex_qa() -> dict[str, Any]:
    return _load(CODEX_QA)


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


def test_codex_qa_is_driven_by_workflow_run_not_issue_comment(
    codex_qa: dict[str, Any],
) -> None:
    """TC-U-034（REQ-015） 自动评审必须挂 `workflow_run`，**不能**挂 `issue_comment`。

    这是本仓库最容易踩、也最容易被「顺手改回去」的坑：`ci.yml` 里那句 `@codex` 评论是
    用 `GITHUB_TOKEN` 发的，而 `GITHUB_TOKEN` 产生的事件**不会启动新的 workflow run**
    （防递归触发）。监听它等于监听一个永不触发的事件——运行记录一片空白、check 全绿，
    唯一的症状是「Codex 从不出现」，而症状本身没有任何地方会报错。

    同一条规则这一轮已经咬过两次，所以这里钉死：换触发方式必须先让这条用例红一次。
    """
    triggers = _on(codex_qa, CODEX_QA)

    assert "workflow_run" in triggers
    assert "issue_comment" not in triggers
    assert "pull_request" not in triggers
    assert triggers["workflow_run"]["workflows"] == ["CI"]


def test_codex_qa_only_follows_a_green_push_to_bugfix(codex_qa: dict[str, Any]) -> None:
    """TC-U-034（REQ-015） 只在「push 到 `bugfix` 且 `ci.yml` 全绿」之后请人评审。

    门禁红的时候不该请人评审——先让门禁说了算。三条限定缺一条，这个 job 就会在
    不该跑的时候跑（白烧 token），或在该跑的时候不跑。
    """
    condition = codex_qa["jobs"]["review"]["if"]

    assert isinstance(condition, str)
    assert "workflow_run.conclusion == 'success'" in condition
    assert "workflow_run.head_branch == 'bugfix'" in condition
    assert "workflow_run.event == 'push'" in condition
    assert "${{" not in condition, "`if` 里不该有未展开的 ${{ }}"


def test_codex_qa_cannot_write_contents(codex_qa: dict[str, Any]) -> None:
    """TC-U-034（REQ-015） 自动评审没有写权限——**自动合并的授权不在这里**。

    合并 `development` 是 QA（Codex）的决定，要人明确点头之后才加。把这条断言写下来，
    是为了让「顺手把 contents 提成 write，好让它自动合并」这个改动必须先让测试红一次，
    而不是在某个深夜悄悄发生。
    """
    permissions = codex_qa["permissions"]

    assert permissions.get("contents") == "read"


def test_codex_qa_reviews_in_a_read_only_sandbox(codex_qa: dict[str, Any]) -> None:
    """TC-U-034（REQ-015） 评审跑在只读沙箱里，且带超时刹车。

    评审是只读活动，给它写权限没有任何正当理由；而这是按 token 计费的，
    没有 `timeout-minutes` 就等于没有刹车。
    """
    job = codex_qa["jobs"]["review"]
    script = "\n".join(str(step.get("run", "")) for step in job["steps"])

    assert "-s read-only" in script
    assert "--dangerously-bypass-approvals-and-sandbox" not in script
    assert job.get("timeout-minutes")
