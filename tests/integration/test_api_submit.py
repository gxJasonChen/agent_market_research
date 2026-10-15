"""提交接口 `POST /api/crew` 的集成用例（spec/040 §2.3）。

用例编号：TC-I-001 / TC-I-002 / TC-I-003。实现全部离线：内存仓储 + 内存队列。
"""

from __future__ import annotations

import statistics
import time
import uuid

import pytest

from amr.domain.status import JobStatus
from tests.fakes import RecordingTaskQueue
from tests.integration.conftest import MakeApp


def _coverage_active() -> bool:
    """当前是否跑在 coverage 插桩下（`make coverage`）。"""
    try:
        from coverage import Coverage
    except ImportError:  # pragma: no cover - 没有 coverage 就是没插桩
        return False
    return Coverage.current() is not None


# ------------------------------------------------------------------ TC-I-001


def test_submit_returns_202_and_uuid_job_id(make_app: MakeApp, payload: dict[str, str]) -> None:
    """TC-I-001 受理成功：202 + `{"job_id": <uuid4>}`（REQ-001）。"""
    harness = make_app()

    response = harness.client.post("/api/crew", json=payload)

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"job_id"}
    assert uuid.UUID(body["job_id"]).version == 4


def test_submit_persists_pending_job_before_enqueue(
    make_app: MakeApp, payload: dict[str, str]
) -> None:
    """TC-I-001 语义保证 1：返回 202 时作业已 PENDING 落库**且**已入队（REQ-001）。

    这里让队列「只记录不执行」，才看得见两者之间的中间状态。
    """
    queue = RecordingTaskQueue()
    harness = make_app(queue=queue)

    response = harness.client.post("/api/crew", json=payload)

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    job = harness.repository.get(job_id)
    assert job is not None
    assert job.status is JobStatus.PENDING
    assert job.result is None
    assert [enqueued_id for enqueued_id, _ in queue.enqueued] == [job_id]


# ------------------------------------------------------------------ TC-I-002


def test_submit_latency_p95_under_200ms(make_app: MakeApp, payload: dict[str, str]) -> None:
    """TC-I-002 接口不等待推理：20 次调用的 P95 < 200ms（NFR-005 / REQ-002）。

    队列用「只记录不执行」的替身：量的是接入路径本身，不含 Flow 执行。

    **先预热一次再计时**：Starlette 在首个请求上才构建中间件栈（外加 TestClient 的
    事件循环门户），一个 hello-world 应用的首次请求也要几百毫秒——那是进程级的一次性
    初始化，不是「受理一次作业」的延迟，NFR-005 量的是后者。阈值仍是 200ms，没有放宽。

    **覆盖率插桩下跳过**：逐行插桩会把每次调用的耗时放大数倍（实测 4s → 287s），
    那时量到的是插桩开销，不是接口延迟。放宽阈值等于把这条需求作废，所以选择跳过并留下
    原因——NFR-005 由 `make test`（无插桩）负责验证，CI 两步都跑。
    """
    if _coverage_active():
        pytest.skip("覆盖率插桩会放大单次调用耗时，本用例在插桩下不成立；见 make test")

    queue = RecordingTaskQueue()
    harness = make_app(queue=queue)
    warmup = harness.client.post("/api/crew", json=payload)
    assert warmup.status_code == 202, "预热请求本身也必须被正常受理"

    latencies_ms: list[float] = []
    for _ in range(20):
        started = time.perf_counter()
        response = harness.client.post("/api/crew", json=payload)
        latencies_ms.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 202

    # 最近秩法 P95：⌈0.95 × 20⌉ = 19，即 n=20 的第 19 个分位切点
    p95 = statistics.quantiles(latencies_ms, n=20)[18]
    assert p95 < 200, f"提交接口 P95={p95:.1f}ms（20 次调用）超出 NFR-005 的 200ms"


# ------------------------------------------------------------------ TC-I-003


@pytest.mark.parametrize(
    ("case", "invalid"),
    [
        (
            "过短",
            {
                "customer_domain": "ab",
                "project_description": "为一家 AI 基础设施公司制定新品发布营销方案",
            },
        ),
        ("全空白", {"customer_domain": "openai.com", "project_description": " " * 12}),
        ("超长", {"customer_domain": "openai.com", "project_description": "长" * 4001}),
    ],
    ids=["too-short", "blank", "too-long"],
)
def test_invalid_payload_returns_422(make_app: MakeApp, case: str, invalid: dict[str, str]) -> None:
    """TC-I-003 入参非法 → 422（FastAPI 默认数组形状，REQ-010）。"""
    queue = RecordingTaskQueue()
    harness = make_app(queue=queue)

    response = harness.client.post("/api/crew", json=invalid)

    assert response.status_code == 422, f"{case}样例应当被拒"
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    # 非法入参既不落库也不入队——校验必须挡在作业创建之前
    assert queue.enqueued == []
    assert harness.repository.list_recent() == []


def test_unknown_field_is_rejected(make_app: MakeApp, payload: dict[str, str]) -> None:
    """TC-I-003 补充：请求体只认 `CrewInputs` 的字段，多给的字段被拒（REQ-010）。"""
    harness = make_app()

    response = harness.client.post("/api/crew", json={**payload, "unexpected": "x"})

    assert response.status_code == 422
