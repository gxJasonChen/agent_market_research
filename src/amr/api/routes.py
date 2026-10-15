"""HTTP 路由（spec/022）。

路由只做三件事：取依赖 → 调服务 → 翻译成响应体。**不写业务规则**（spec/010 §3）：
状态机能写什么由 `JobService` 决定，响应长什么样由 `api/schemas.py` 决定。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, status

import amr
from amr.api.deps import get_job_service
from amr.api.schemas import HealthResponse, JobResponse, SubmitResponse, to_job_response
from amr.domain.inputs import CrewInputs
from amr.services.job_service import JobService

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/api/crew",
    response_model=SubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交一次调研作业",
)
def submit_crew(
    payload: CrewInputs,
    service: JobService = Depends(get_job_service),
) -> SubmitResponse:
    """CT-001：受理成功返回 202 与 `job_id`（REQ-001）。

    请求体直接用领域模型 `CrewInputs`：长度、strip、多余字段的规则只有那一份（REQ-010），
    路由不另写一套校验。接口不等待推理——入队即返回（REQ-002）。
    """
    job_id = service.create(payload)
    # 日志只记 job_id：请求体是不可信输入，可能夹带敏感内容（spec/000 §7）
    logger.info("已受理作业 job_id=%s", job_id)
    return SubmitResponse(job_id=job_id)


@router.get("/api/crew/{job_id}", response_model=JobResponse, summary="查询作业状态与结果")
def get_crew_job(
    job_id: str,
    service: JobService = Depends(get_job_service),
) -> JobResponse:
    """CT-002：作业不存在时抛 `JobNotFoundError`，由异常处理器映射 404（REQ-003）。"""
    return to_job_response(service.get(job_id))


@router.get("/healthz", response_model=HealthResponse, summary="进程健康检查")
def healthz() -> HealthResponse:
    """CT-003：只反映进程存活，不探测 MySQL / Redis（REQ-014）。

    探活不该把负载打到下游——依赖健康的验证交给系统测试与运维手段。
    """
    return HealthResponse(status="ok", version=amr.__version__)
