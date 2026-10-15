"""`CeleryTaskQueue` 的错误翻译（Codex review M5）。

守护需求：REQ-011 —— 队列不可用 → `QueueUnavailableError` → HTTP 503。

**为什么必须有这组用例**：集成测试里的 `FailingTaskQueue` 自己就抛
`QueueUnavailableError`，等于把「broker 异常怎么翻译成领域异常」这段唯一的翻译代码
整个绕过去了——真正可能写错的地方反倒无人守护。

用例编号：TC-U-029。
"""

from __future__ import annotations

from typing import Any

import pytest
from celery import Celery

from amr.adapters.celery_queue import KICKOFF_TASK_NAME, CeleryTaskQueue
from amr.domain.errors import QueueUnavailableError
from amr.domain.inputs import CrewInputs

INPUTS = CrewInputs(
    customer_domain="openai.com",
    project_description="为一家 AI 基础设施公司制定新品发布营销方案",
)


class _BoomTask:
    """已注册但投递必炸的任务（模拟 broker 不可达）。"""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def apply_async(self, *_: Any, **__: Any) -> None:
        raise self._error


def _app_with(task: Any, name: str = KICKOFF_TASK_NAME) -> Celery:
    app = Celery("test", broker="memory://")
    app.tasks[name] = task
    return app


# ------------------------------------------------------------------ TC-U-029


def test_unregistered_task_is_reported_as_queue_unavailable() -> None:
    """TC-U-029 任务没注册 = 没人会消费这条消息，等同队列不可用（REQ-011）。

    刻意用一个**绝不存在的任务名**而不是「空 app」：Celery 的 task 默认 `shared=True`，
    会注册进所有 app；只要进程里 import 过 `amr.worker.tasks`，
    「这个 app 里没有任务」这个前提就悄悄不成立了——单独跑能过、全量跑就挂。
    """
    app = Celery("test", broker="memory://")
    queue = CeleryTaskQueue(app, task_name="amr.definitely.not.registered")

    with pytest.raises(QueueUnavailableError) as exc:
        queue.enqueue_flow("job-1", INPUTS)

    assert exc.value.context.get("task") == "amr.definitely.not.registered"
    assert exc.value.context.get("job_id") == "job-1"


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("Cannot connect to redis://:hunter2@cache.internal:6379/0"),
        OSError("Connection refused"),
        RuntimeError("broker 挂了"),
    ],
)
def test_broker_failure_is_translated(error: BaseException) -> None:
    """TC-U-029 broker 抛什么都要翻译成 `QueueUnavailableError`（REQ-011）。

    否则路由的异常映射表认不出它，调用方拿到的是 500 而不是 503。
    """
    queue = CeleryTaskQueue(_app_with(_BoomTask(error)))

    with pytest.raises(QueueUnavailableError):
        queue.enqueue_flow("job-1", INPUTS)


def test_broker_failure_does_not_leak_connection_string(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """TC-U-029 异常与日志都不得带出 broker 连接串（REQ-009）。

    kombu 的连接异常里常常直接带着完整 URL（含口令），照抄进日志就等于泄露。
    """
    import logging

    secret_url = "redis://:hunter2@cache.internal:6379/0"
    queue = CeleryTaskQueue(
        _app_with(_BoomTask(ConnectionError(f"Cannot connect to {secret_url}")))
    )

    with caplog.at_level(logging.ERROR), pytest.raises(QueueUnavailableError) as exc:
        queue.enqueue_flow("job-1", INPUTS)

    assert "hunter2" not in str(exc.value)
    assert "hunter2" not in caplog.text


def test_successful_enqueue_passes_job_id_and_inputs() -> None:
    """TC-U-029 正常投递：消息体只有 job_id 与入参（状态以仓储为准）（REQ-001）。"""

    class _RecordingTask:
        def __init__(self) -> None:
            self.calls: list[tuple[list[Any], dict[str, Any]]] = []

        def apply_async(self, args: list[Any], **kwargs: Any) -> None:
            self.calls.append((args, kwargs))

    task = _RecordingTask()
    queue = CeleryTaskQueue(_app_with(task))

    queue.enqueue_flow("job-42", INPUTS)

    assert len(task.calls) == 1
    args, _ = task.calls[0]
    assert args[0] == "job-42"
    assert args[1] == INPUTS.model_dump(mode="json")
