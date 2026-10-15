"""查询接口 `GET /api/crew/{job_id}`、`/healthz` 与错误语义的集成用例（spec/040 §2.3）。

用例编号：TC-I-004 ~ TC-I-007、TC-I-010 ~ TC-I-012。
"""

from __future__ import annotations

import json

import pytest

import amr
from amr.domain.errors import IllegalTransitionError, RepositoryUnavailableError
from amr.domain.inputs import CrewInputs
from amr.domain.status import JobStatus
from tests.fakes import FAILING_MESSAGE, FailingFlowRunner, FailingTaskQueue, RecordingTaskQueue
from tests.integration.conftest import ExplodingRepository, MakeApp

#: 危险文案：堆栈、连接串、口令。**任何**响应体都不许出现它们（CT-005 / REQ-009）
LEAKY_MESSAGE = (
    "Traceback (most recent call last): mysql.connector.errors.InterfaceError: "
    "mysql://root:sup3rsecret@10.0.0.5:3306/amr password=sup3rsecret"
)
LEAK_MARKERS = ("traceback", "mysql", "password", "secret", "select ", "10.0.0.5")


# ------------------------------------------------------------------ TC-I-004


def test_query_unknown_job_returns_404(make_app: MakeApp) -> None:
    """TC-I-004 作业不存在 → 404 且带 `detail`（REQ-003）。"""
    harness = make_app()

    response = harness.client.get("/api/crew/definitely-not-a-job")

    assert response.status_code == 404
    assert response.json()["detail"]


# ------------------------------------------------------------------ TC-I-005


def test_query_existing_job_returns_full_shape(make_app: MakeApp, payload: dict[str, str]) -> None:
    """TC-I-005 已存在的作业 → 200 且四个字段齐全、事件正序（REQ-003 / REQ-005）。"""
    harness = make_app()
    job_id = harness.client.post("/api/crew", json=payload).json()["job_id"]

    response = harness.client.get(f"/api/crew/{job_id}")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"job_id", "status", "result", "events"}
    assert body["job_id"] == job_id
    assert body["status"] in {status.value for status in JobStatus}
    assert body["events"], "执行过的作业应当留下事件流水"
    assert all(set(event) == {"timestamp", "data"} for event in body["events"])
    timestamps = [event["timestamp"] for event in body["events"]]
    assert timestamps == sorted(timestamps), "事件必须按时间正序（REQ-005）"


def test_query_complete_job_returns_structured_result(
    make_app: MakeApp, payload: dict[str, str]
) -> None:
    """TC-I-005 补充：`COMPLETE` 的 `result` 是**结构化对象**而不是裸文本（REQ-006）。"""
    harness = make_app()
    job_id = harness.client.post("/api/crew", json=payload).json()["job_id"]

    body = harness.client.get(f"/api/crew/{job_id}").json()

    assert body["status"] == "COMPLETE"
    result = body["result"]
    assert isinstance(result, dict), "COMPLETE 的 result 必须是对象（CT-002 / REQ-006）"
    assert set(result) == {"research_report", "strategy", "campaign_idea", "copy"}


# ------------------------------------------------------------------ TC-I-006


def test_queue_unavailable_returns_503_and_job_is_error(
    make_app: MakeApp, payload: dict[str, str]
) -> None:
    """TC-I-006 队列不可用 → 503，且作业被置 ERROR，**不留僵尸 PENDING**（REQ-011）。"""
    queue = FailingTaskQueue()
    harness = make_app(queue=queue)

    response = harness.client.post("/api/crew", json=payload)

    assert response.status_code == 503
    assert response.json()["detail"]
    # 503 的响应体不回显 job_id，从替身记录里取；再走一次 HTTP 查询确认落库状态
    job_id = queue.attempts[0]
    job = harness.client.get(f"/api/crew/{job_id}").json()
    assert job["status"] == "ERROR"
    assert job["result"], "失败作业必须留下错误摘要"


# ------------------------------------------------------------------ TC-I-007


def test_repository_unavailable_returns_503(make_app: MakeApp, payload: dict[str, str]) -> None:
    """TC-I-007 存储不可用 → 503（REQ-011）。"""
    repository = ExplodingRepository(RepositoryUnavailableError("存储不可用"))
    harness = make_app(repository=repository)

    response = harness.client.post("/api/crew", json=payload)

    assert response.status_code == 503
    assert response.json()["detail"]


# ------------------------------------------------------------------ TC-I-010


def test_event_pipeline_is_complete_and_ordered(
    make_app: MakeApp, payload: dict[str, str], inputs: CrewInputs
) -> None:
    """TC-I-010 事件流水覆盖「受理 → 入队 → 开始 → 各 Task → 完成」且正序（REQ-005 / REQ-013）。

    队列用「只记录不执行」的替身，于是「入队」与「执行」分离——这正是异步生产的形态
    （`InlineTaskQueue` 会把两件事压进同一次调用，反而看不出阶段顺序）。
    """
    queue = RecordingTaskQueue()
    harness = make_app(queue=queue)
    job_id = harness.client.post("/api/crew", json=payload).json()["job_id"]

    harness.executor.execute(job_id, inputs)  # 相当于 Worker 消费到这条消息

    events = harness.client.get(f"/api/crew/{job_id}").json()["events"]
    assert [event["data"] for event in events] == [
        "作业已受理",
        "作业已入队",
        "Flow 开始执行",
        "调研完成",
        "战略完成",
        "Flow 执行完成",
    ]
    timestamps = [event["timestamp"] for event in events]
    assert timestamps == sorted(timestamps)


# ------------------------------------------------------------------ TC-I-011


def test_healthz_reports_process_alive(make_app: MakeApp) -> None:
    """TC-I-011 `/healthz` 只反映进程存活：200 + status/version（REQ-014）。"""
    harness = make_app()

    response = harness.client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": amr.__version__}


# ------------------------------------------------------------------ TC-I-012


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RuntimeError(LEAKY_MESSAGE), id="unexpected-error"),
        pytest.param(IllegalTransitionError(LEAKY_MESSAGE), id="amr-error"),
    ],
)
def test_error_body_does_not_leak_internals(
    make_app: MakeApp, payload: dict[str, str], error: BaseException
) -> None:
    """TC-I-012 500 只给通用文案：响应体不含堆栈 / SQL / 连接串 / 口令（REQ-009 / REQ-011）。"""
    harness = make_app(
        repository=ExplodingRepository(error),
        raise_server_exceptions=False,  # 要的是响应体，不是把异常抛回测试
    )

    response = harness.client.post("/api/crew", json=payload)

    assert response.status_code == 500
    assert response.json() == {"detail": "内部错误"}
    leaked = response.text.lower()
    for marker in LEAK_MARKERS:
        assert marker not in leaked, f"响应体泄露了内部信息：{marker}"


# ------------------------------------------------------------------ 补充


def test_failed_job_result_is_not_parsed_as_json(
    make_app: MakeApp, payload: dict[str, str]
) -> None:
    """TC-I-005 补充：`ERROR` 的 `result` 是错误摘要**字符串**，不做 JSON 解析（CT-002）。"""
    harness = make_app(runner=FailingFlowRunner())
    job_id = harness.client.post("/api/crew", json=payload).json()["job_id"]

    body = harness.client.get(f"/api/crew/{job_id}").json()

    assert body["status"] == "ERROR"
    assert body["result"] == FAILING_MESSAGE
    assert not isinstance(body["result"], dict)


def test_query_shape_is_stable_between_polls(make_app: MakeApp, payload: dict[str, str]) -> None:
    """TC-I-005 补充：终态后重复查询必须逐字一致（CT-002 语义保证 2，REQ-004）。"""
    harness = make_app()
    job_id = harness.client.post("/api/crew", json=payload).json()["job_id"]

    snapshots = [
        json.dumps(harness.client.get(f"/api/crew/{job_id}").json(), sort_keys=True)
        for _ in range(3)
    ]

    assert len(set(snapshots)) == 1
