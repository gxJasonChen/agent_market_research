"""作业状态机契约测试。

守护需求：REQ-004（终态不可变 + 失败优先）。
对应规格：spec/020-contracts-domain.md §1.2 真值表。
用例编号：TC-U-001 ~ TC-U-004。
"""

from __future__ import annotations

import pytest

from amr.domain.status import ALLOWED_TRANSITIONS, TERMINAL, JobStatus, can_transition, is_terminal

ALL = list(JobStatus)

# spec/020 §1.2 的真值表，逐格写死在这里。
# 这张表就是「源项目失败作业被误报成功」缺陷的防线：改任何一格都会让测试炸掉。
TRUTH_TABLE: dict[tuple[JobStatus, JobStatus], bool] = {
    (JobStatus.PENDING, JobStatus.PENDING): True,  # 非终态幂等
    (JobStatus.PENDING, JobStatus.RUNNING): True,
    (JobStatus.PENDING, JobStatus.COMPLETE): False,  # 必须先 RUNNING
    (JobStatus.PENDING, JobStatus.ERROR): True,
    (JobStatus.RUNNING, JobStatus.PENDING): False,  # 不能回退
    (JobStatus.RUNNING, JobStatus.RUNNING): True,  # 非终态幂等
    (JobStatus.RUNNING, JobStatus.COMPLETE): True,
    (JobStatus.RUNNING, JobStatus.ERROR): True,
    (JobStatus.COMPLETE, JobStatus.PENDING): False,
    (JobStatus.COMPLETE, JobStatus.RUNNING): False,
    (JobStatus.COMPLETE, JobStatus.COMPLETE): False,  # 终态不可变
    (JobStatus.COMPLETE, JobStatus.ERROR): False,
    (JobStatus.ERROR, JobStatus.PENDING): False,
    (JobStatus.ERROR, JobStatus.RUNNING): False,
    (JobStatus.ERROR, JobStatus.COMPLETE): False,  # ← 源项目缺陷所在的那一格
    (JobStatus.ERROR, JobStatus.ERROR): False,  # 终态不可变
}


def test_truth_table_is_complete_16_cells() -> None:
    """TC-U-001 前置：真值表必须覆盖 4×4=16 种组合，防止漏格。"""
    assert len(TRUTH_TABLE) == 16
    assert set(TRUTH_TABLE) == {(src, dst) for src in ALL for dst in ALL}


@pytest.mark.parametrize(
    ("src", "dst"), sorted(TRUTH_TABLE, key=lambda p: (p[0].value, p[1].value))
)
def test_can_transition_matches_truth_table(src: JobStatus, dst: JobStatus) -> None:
    """TC-U-001 状态迁移真值表穷举（REQ-004）。"""
    assert can_transition(src, dst) is TRUTH_TABLE[(src, dst)]


def test_error_cannot_be_overwritten_by_complete() -> None:
    """TC-U-002 失败作业永远不能被改写成成功（REQ-004）。

    这是源项目严重缺陷的回归测试：异常分支写入 ERROR 后，
    成功分支又无条件写入 COMPLETE，导致失败作业对外显示为完成。
    """
    assert can_transition(JobStatus.ERROR, JobStatus.COMPLETE) is False


@pytest.mark.parametrize("terminal", sorted(TERMINAL, key=lambda s: s.value))
@pytest.mark.parametrize("dst", ALL, ids=lambda s: s.value)
def test_terminal_states_are_immutable(terminal: JobStatus, dst: JobStatus) -> None:
    """TC-U-003 终态对任何目标状态都不可写，包括写回自身（REQ-004）。"""
    assert can_transition(terminal, dst) is False


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (JobStatus.PENDING, JobStatus.PENDING),
        (JobStatus.RUNNING, JobStatus.RUNNING),
    ],
)
def test_non_terminal_same_status_is_idempotent(src: JobStatus, dst: JobStatus) -> None:
    """TC-U-004 非终态重复写同一状态视为幂等成功（REQ-004）。"""
    assert can_transition(src, dst) is True


@pytest.mark.parametrize("status", ALL, ids=lambda s: s.value)
def test_is_terminal(status: JobStatus) -> None:
    """TC-U-003 补充：终态判定与 TERMINAL 集合一致（REQ-004）。"""
    assert is_terminal(status) is (status in TERMINAL)


def test_terminal_set_is_exactly_complete_and_error() -> None:
    """TC-U-003 补充：终态集合只有 COMPLETE / ERROR（REQ-004）。"""
    expected = frozenset({JobStatus.COMPLETE, JobStatus.ERROR})
    assert expected == TERMINAL


def test_allowed_transitions_table_matches_spec() -> None:
    """TC-U-001 补充：ALLOWED_TRANSITIONS 必须与 spec/020 §1.1 定义一致（REQ-004）。"""
    expected = {
        JobStatus.PENDING: frozenset({JobStatus.RUNNING, JobStatus.ERROR}),
        JobStatus.RUNNING: frozenset({JobStatus.COMPLETE, JobStatus.ERROR}),
        JobStatus.COMPLETE: frozenset(),
        JobStatus.ERROR: frozenset(),
    }
    assert expected == ALLOWED_TRANSITIONS


def test_job_status_is_str_enum_with_spec_values() -> None:
    """TC-U-001 补充：状态取值是字符串枚举，序列化后即契约字面量（REQ-004 / CT-002）。"""
    assert [s.value for s in JobStatus] == ["PENDING", "RUNNING", "COMPLETE", "ERROR"]
    assert JobStatus.PENDING == "PENDING"
