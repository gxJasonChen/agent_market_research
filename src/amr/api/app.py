"""FastAPI 应用工厂（spec/021 §7、spec/022）。

`create_app` 是**纯函数工厂**：入参相同就得到等价的应用，没有隐藏的全局状态，
也不在 import 期碰任何外部系统。全部依赖可注入——集成测试注入内存实现即可离线跑通
整层（NFR-001）。异常到状态码的映射集中在 `_HANDLERS` 一处（CT-005）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import cast

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import amr
from amr.api.deps import build_dependencies, install_dependencies
from amr.api.routes import router
from amr.config import Settings, configure_logging, export_third_party_env, get_settings
from amr.domain.errors import (
    AmrError,
    InvalidInputError,
    JobNotFoundError,
    QueueUnavailableError,
    RepositoryUnavailableError,
)
from amr.ports.clock import Clock, IdGenerator
from amr.ports.queue import TaskQueue
from amr.ports.repository import JobRepository

__all__ = ["JsonResponse", "create_app"]

logger = logging.getLogger(__name__)

#: CT-005：错误体里只允许出现这些**固定文案**，不得带堆栈、SQL、连接串、口令
DETAIL_JOB_NOT_FOUND = "job not found"
DETAIL_QUEUE_UNAVAILABLE = "任务队列不可用"
DETAIL_REPOSITORY_UNAVAILABLE = "存储不可用"
DETAIL_INTERNAL = "内部错误"


class JsonResponse(JSONResponse):
    """把 JSON 响应的内容类型钉成 `application/json; charset=utf-8`（CT-022）。

    Starlette 只给 `text/*` 补 charset，JSON 默认是裸的 `application/json`。契约把
    内容类型写成了带 charset 的形式，这里对齐它——`/docs` 与实际响应因此不会各说各话。
    """

    media_type = "application/json; charset=utf-8"


def create_app(
    settings: Settings | None = None,
    *,
    repository: JobRepository | None = None,
    queue: TaskQueue | None = None,
    clock: Clock | None = None,
    ids: IdGenerator | None = None,
) -> FastAPI:
    """构造应用。依赖全部可注入；不传时按 `Settings` 装配生产实现。

    生产装配**推迟到首个请求**（见 `api/deps.py`）：`import amr.api.app` 会被
    `uvicorn amr.api.app:app` 在启动时执行，那一刻不该连 MySQL、也不该加载 celery。
    """
    resolved = settings if settings is not None else get_settings()
    configure_logging(resolved)
    export_third_party_env(resolved)

    application = FastAPI(
        title="多智能体市场调研与营销内容生成",
        version=amr.__version__,
        default_response_class=JsonResponse,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origins,  # 默认不是 "*"（CT-004）
        allow_credentials=False,  # 与 "*" 同用是无效且不安全的组合，这里从根上避免
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    install_dependencies(
        application,
        lambda: build_dependencies(
            resolved, repository=repository, queue=queue, clock=clock, ids=ids
        ),
    )
    application.include_router(router)
    for exc_type, handler in _HANDLERS.items():
        application.add_exception_handler(exc_type, handler)

    return application


# ---------------------------------------------------------------- 异常处理器（CT-005）


def _error(status_code: int, detail: str) -> JsonResponse:
    return JsonResponse(status_code=status_code, content={"detail": detail})


async def _handle_job_not_found(_request: Request, _exc: Exception) -> JsonResponse:
    """作业不存在 → 404（CT-002）。

    文案取自契约常量而不是领域异常：领域异常的 `str()` 带上下文，透出等于曝光内部结构。
    """
    return _error(status.HTTP_404_NOT_FOUND, DETAIL_JOB_NOT_FOUND)


async def _handle_queue_unavailable(request: Request, exc: Exception) -> JsonResponse:
    """队列不可用 → 503（CT-001）。作业此时已被置 ERROR，不会留下僵尸 PENDING。"""
    logger.error("任务队列不可用 path=%s reason=%s", request.url.path, type(exc).__name__)
    return _error(status.HTTP_503_SERVICE_UNAVAILABLE, DETAIL_QUEUE_UNAVAILABLE)


async def _handle_repository_unavailable(request: Request, exc: Exception) -> JsonResponse:
    """存储不可用 → 503（CT-002）。"""
    logger.error("存储不可用 path=%s reason=%s", request.url.path, type(exc).__name__)
    return _error(status.HTTP_503_SERVICE_UNAVAILABLE, DETAIL_REPOSITORY_UNAVAILABLE)


async def _handle_invalid_input(_request: Request, exc: Exception) -> JsonResponse:
    """领域层输入非法 → 422。消息由领域层给出（如「缺少环境变量 X」），不含其值。"""
    return _error(status.HTTP_422_UNPROCESSABLE_ENTITY, cast(AmrError, exc).message)


async def _handle_internal(request: Request, exc: Exception) -> JsonResponse:
    """其余业务异常与一切未预期异常 → 500，响应体只有通用文案。

    异常照常进日志（否则线上无法排障），但**绝不**进响应体：CT-005 禁止堆栈、
    SQL、连接串、口令出现在响应里（REQ-009）。
    """
    logger.exception("未处理的异常 path=%s error=%s", request.url.path, type(exc).__name__)
    return _error(status.HTTP_500_INTERNAL_SERVER_ERROR, DETAIL_INTERNAL)


#: 异常类型 → 处理器。Starlette 按异常类型的 MRO 查找，因此更具体的类型总会被命中
_HANDLERS: dict[type[Exception], Callable[[Request, Exception], Awaitable[JsonResponse]]] = {
    JobNotFoundError: _handle_job_not_found,
    QueueUnavailableError: _handle_queue_unavailable,
    RepositoryUnavailableError: _handle_repository_unavailable,
    InvalidInputError: _handle_invalid_input,
    AmrError: _handle_internal,  # 其余业务异常：编程错误或并发冲突，500
    Exception: _handle_internal,  # 兜底：任何未预期异常都不得把内部细节吐出去
}


# 刻意**不**在模块级调用 `create_app()`。
#
# 那样做的话，`import amr.api.app` 这个动作本身就会读取 `.env`：任何一个取值非法
# （`MYSQL_PORT=abc`、`AMR_LLM_PROVIDER=xxx`）都会让 import 直接抛 ValidationError，
# 影响面远不止真实进程——测试收集、文档工具、任何一次误 import 都会炸。
#
# 因此 ASGI 入口用工厂模式：`uvicorn amr.api.app:create_app --factory`。
