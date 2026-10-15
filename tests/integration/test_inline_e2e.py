"""`InlineTaskQueue` 同步链路（提交 → 执行 → 落库）的集成用例（spec/040 §2.3）。

用例编号：TC-I-008 / TC-I-009。没有 Redis、没有 Worker 进程，但跑的仍是
`JobService` + `FlowExecutor` + `RepositoryEventPublisher` 这套生产代码。
"""

from __future__ import annotations

from amr.domain.inputs import CrewInputs
from amr.domain.models import FlowResult
from amr.domain.status import JobStatus
from tests.fakes import FAILING_MESSAGE, FailingFlowRunner, FakeFlowRunner
from tests.integration.conftest import MakeApp

# ------------------------------------------------------------------ TC-I-008


def test_inline_pipeline_reaches_complete_with_structured_result(
    make_app: MakeApp, payload: dict[str, str], inputs: CrewInputs
) -> None:
    """TC-I-008 同步端到端：提交即 COMPLETE，`result` 可 `FlowResult.from_json`（REQ-002/003/006）。"""
    runner = FakeFlowRunner()
    harness = make_app(runner=runner)

    submitted = harness.client.post("/api/crew", json=payload)
    assert submitted.status_code == 202
    job_id = submitted.json()["job_id"]

    body = harness.client.get(f"/api/crew/{job_id}").json()

    assert body["status"] == "COMPLETE"
    assert body["events"], "同步链路也必须留下事件流水"
    assert runner.calls == [(job_id, inputs)], "执行器拿到的必须是提交时的入参"
    assert FlowResult.from_json(body["result"]) == runner.result


def test_inline_pipeline_persists_result_in_repository(
    make_app: MakeApp, payload: dict[str, str]
) -> None:
    """TC-I-008 补充：`result` 真的落库了，而不是只在响应里拼出来（REQ-003）。"""
    harness = make_app()

    job_id = harness.client.post("/api/crew", json=payload).json()["job_id"]

    job = harness.repository.get(job_id)
    assert job is not None
    assert job.status is JobStatus.COMPLETE
    assert job.result is not None
    assert FlowResult.from_json(job.result).research_report == "调研报告：测试用"


# ------------------------------------------------------------------ TC-I-009


def test_failing_runner_never_reports_complete(make_app: MakeApp, payload: dict[str, str]) -> None:
    """TC-I-009 失败作业不可被误报成功：连续查询恒为 ERROR、结果一致（REQ-004）。

    这是源项目缺陷形状的端到端回归：异常分支写 ERROR 之后，成功分支又被执行。
    """
    harness = make_app(runner=FailingFlowRunner())
    submitted = harness.client.post("/api/crew", json=payload)
    assert submitted.status_code == 202
    job_id = submitted.json()["job_id"]

    snapshots = [harness.client.get(f"/api/crew/{job_id}").json() for _ in range(3)]

    assert [snapshot["status"] for snapshot in snapshots] == ["ERROR", "ERROR", "ERROR"]
    assert {snapshot["result"] for snapshot in snapshots} == {FAILING_MESSAGE}
    events = [event["data"] for event in snapshots[0]["events"]]
    assert "Flow 执行完成" not in events, "失败作业绝不能留下「执行完成」事件"

    job = harness.repository.get(job_id)
    assert job is not None
    assert job.status is JobStatus.ERROR
