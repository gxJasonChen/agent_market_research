"""`TaskQueue` 的同进程实现（spec/021 §2）。

集成测试用它把「提交 → 执行 → 落库」压进一次调用：没有 Redis、没有 Worker 进程，
整条链路照样可以断言（NFR-001）。

**刻意不 import `amr.services`**：适配层不认识服务层。执行单元以回调形式注入，
调用方拿 `FlowExecutor.execute` 绑进来即可——适配器因此对「执行的是什么」一无所知。
"""

from __future__ import annotations

from collections.abc import Callable

from amr.domain.inputs import CrewInputs

__all__ = ["InlineTaskQueue"]


class InlineTaskQueue:
    """在调用方线程里同步执行，不经过任何 broker。"""

    def __init__(self, execute: Callable[[str, CrewInputs], None]) -> None:
        """`execute` 即 `FlowExecutor.execute` 的签名（spec/021 §3）。"""
        self._execute = execute

    def enqueue_flow(self, job_id: str, inputs: CrewInputs) -> None:
        """同步执行一次。

        执行期的异常**不**包装成 `QueueUnavailableError`：队列没坏，坏的是作业本身，
        而作业的失败已经由 `FlowExecutor` 落成 `ERROR` 状态（REQ-004）。把两者混为一谈
        会让「队列不可用」这条 503 语义失去意义。
        """
        self._execute(job_id, inputs)
